"""B04.5 (#759): a protected MCP tool call reaches the server only through the dispatcher.

An MCP server is untrusted. A workflow ``tool`` node bound to an MCP tool, and an MCP node, are classified from the **server
record**, the administrator's explicit tool policy for exactly that tool, and the protected-effect inventory: never from what the
server reports (annotations such as ``readOnlyHint``, descriptions, results), the binding or the model. A ``read`` tool runs inline
as before; every other call is proposed, approved and sent once by the private ``mcp`` backend under the execution-owner lease; an
unclassified, blocked or unknown tool is refused with a typed, payload-free code before any proposal or approval exists.

The tests drive the real runtime executor, the real dispatcher and the real MCP gateway over a scripted server installed at the
SDK's transport seam (``tests/mcp_fakes.py``), so the policy gate, the address check, the span and the ``never_sent``
bookkeeping are the production code, and every claim is measured at the server (``FakeMcp.calls``).
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.dispatcher as dispatcher_module
import caliber.mcp_gateway as gateway_module
import caliber.workflows.runtime as runtime_module
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberEventOutbox,
    CaliberMcpServer,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects.connectors import mcp_tool
from caliber.effects.dispatcher import BACKENDS, EffectRefused, dispatch, submit_proposal
from caliber.effects.receipts import ReceiptFacts
from caliber.mcp_gateway import McpGatewayTransportError, load_server_config
from caliber.mcp_policy import _reset_rate_limits_for_tests
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from tests.dialect_helpers import DIALECTS
from tests.effect_helpers import leased
from tests.mcp_fakes import (
    CREDENTIAL,
    SERVER_ID,
    FakeMcp,
    seed_server,
    server_policy,
    use_database,
)
from tests.test_registered_tool_dispatch import (
    RUN_ID,
    SENTINEL,
    _world,
    count,
    grant,
    proposals,
    verifier,
)
from tests.workflow_helpers import make_manifest

Authorizer = Callable[[Any], EffectAuthorization | None]


@pytest.fixture
def factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        use_database(monkeypatch, factory)
        yield factory


@pytest.fixture(params=DIALECTS)
def each_dialect(
    request: pytest.FixtureRequest, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    with _world(request.param, tmp_path, monkeypatch) as factory:
        use_database(monkeypatch, factory)
        yield factory


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> FakeMcp:
    _reset_rate_limits_for_tests()
    return FakeMcp().install(monkeypatch)


# ---------------------------------------------------------------- the workflow under test


def tool_node_manifest(tool_name: str, *, level: str = "read") -> dict[str, Any]:
    """A tool node bound to the MCP tool ``tool_name``; ``level`` is what the **binding** claims (not trusted)."""
    data = make_manifest()
    del data["nodes"]["agent"]
    data["nodes"]["tool_node"] = {
        "id": "tool_node",
        "type": "tool",
        "tool_name": "lookup_docs",
        "inputs": {"input": {"type": "string"}, "arguments": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["tools"] = {
        "lookup_docs": {
            "type": "mcp_tool",
            "server_id": SERVER_ID,
            "tool_name": tool_name,
            "side_effect_level": level,
        }
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_node", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_node", "to": "final", "map": {"text": "response"}},
    ]
    return data


def mcp_node_manifest(tool_name: str, *, server_id: str = SERVER_ID) -> dict[str, Any]:
    data = make_manifest()
    del data["nodes"]["agent"]
    data["nodes"]["tool_node"] = {
        "id": "tool_node",
        "type": "mcp_resource",
        "server_id": server_id,
        "tool_name": tool_name,
        "timeout_seconds": 30,
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_node", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_node", "to": "final", "map": {"text": "response"}},
    ]
    return data


class McpRun:
    """One plan over ``tool_node``; ``authorized`` records every proposal the runtime asked to authorize."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        manifest: dict[str, Any],
        *,
        authorize: Authorizer | None = None,
        receipt_verifier: Callable[[Any, dict[str, Any]], Any] = verifier,
        context: bool = True,
        lookup: bool = True,
        preview: bool = False,
    ) -> None:
        self.factory = factory
        self.preview = preview
        self.authorized: list[str] = []
        user_authorize = authorize

        def authorize_and_record(proposal: Any) -> EffectAuthorization | None:
            self.authorized.append(proposal.proposal_id)
            if user_authorize is not None:
                return user_authorize(proposal)
            return grant(factory, proposal.proposal_id)

        resolver = InMemoryToolResolver([])
        self.plan = RuntimePlan(
            ir=build_ir(parse_manifest(manifest), resolver, version="1"),
            resolver=resolver,
            workflow_version_id="version-b04-5",
            registered_tool_identity=(lambda _name, _version: None) if lookup else None,
            governed_effects=(
                GovernedEffectContext(
                    session_factory=factory,
                    workflow_run_id=RUN_ID,
                    authorize=authorize_and_record,
                    receipt_verifier=receipt_verifier,
                )
                if context
                else None
            ),
        )

    def execute(self, text: str = "open an issue", **kwargs: Any) -> Any:
        return execute(
            self.plan, text, executor=FakeWorkflowExecutor(), preview=self.preview, **kwargs
        )


def tool_run(factory: sessionmaker[Session], tool: str = "issue_write", **kwargs: Any) -> McpRun:
    return McpRun(factory, tool_node_manifest(tool), **kwargs)


def node_run(factory: sessionmaker[Session], tool: str = "issue_write", **kwargs: Any) -> McpRun:
    return McpRun(factory, mcp_node_manifest(tool), **kwargs)


def set_policy(factory: sessionmaker[Session], tool: str, policy: dict[str, Any] | None) -> None:
    with factory() as session:
        row = session.get(CaliberMcpServer, SERVER_ID)
        assert row is not None
        policies = dict(row.tool_policies)
        if policy is None:
            policies.pop(tool, None)
        else:
            policies[tool] = policy
        row.tool_policies = policies
        session.commit()


def only_proposal(factory: sessionmaker[Session]) -> CaliberEffectProposal:
    (proposal,) = proposals(factory)
    return proposal


# ---------------------------------------------------------------- governed write calls


@pytest.mark.parametrize("kind", [tool_run, node_run], ids=["tool-node", "mcp-node"])
def test_a_write_tool_reaches_the_server_only_after_proposal_approval_and_dispatch(
    each_dialect: sessionmaker[Session], mcp: FakeMcp, kind: Callable[..., McpRun]
) -> None:
    factory = each_dialect
    seed_server(factory)
    seen_at_call: dict[str, Any] = {}

    def on_call(_name: str, _arguments: dict[str, Any]) -> None:
        with factory() as session:
            row = session.execute(select(CaliberEffectProposal)).scalar_one()
            seen_at_call["state"] = row.state
            seen_at_call["consumed"] = session.execute(
                select(CaliberRuntimeApprovalRequest.consumed_at)
            ).scalar_one()
            seen_at_call["outbox"] = len(
                list(session.execute(select(CaliberEventOutbox)).scalars())
            )

    mcp.on_call = on_call
    mcp.result = {"issue": "I-1"}

    result = kind(factory).execute('{"title": "disk full"}')

    assert result.status == "completed", result.error
    assert mcp.calls == [{"name": "issue_write", "arguments": {"title": "disk full"}}]
    # The server was called only after the claim committed: dispatched, approval consumed, outbox event written.
    assert seen_at_call["state"] == "dispatched"
    assert seen_at_call["consumed"] is not None
    assert seen_at_call["outbox"] == 1
    assert "I-1" in result.output
    proposal = only_proposal(factory)
    assert proposal.state == "committed"
    assert proposal.connector == "mcp-tool"
    assert proposal.resource == f"mcp/{SERVER_ID}/issue_write"
    # An administrator-classified MCP tool is not in the inventory: the recorded class is the fail-closed one.
    assert proposal.effect_class == "irreversible_write"
    assert count(factory, CaliberEffectReceipt) == 1


def test_the_proposal_records_the_server_the_tool_a_digest_and_the_arguments_and_no_credential(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory, env={"API_TOKEN": CREDENTIAL})
    run = tool_run(factory)

    assert run.execute(json.dumps({"title": SENTINEL})).status == "completed"

    proposal = only_proposal(factory)
    with factory() as session:
        recorded = dispatcher_module.read_proposal(session, proposal.proposal_id)
    args = recorded["args"]
    assert set(args) == {"server_id", "tool", "server_digest", "arguments"}
    assert (args["server_id"], args["tool"]) == (SERVER_ID, "issue_write")
    assert args["arguments"] == {"title": SENTINEL}
    assert args["server_digest"].startswith("sha256:")
    serialized = json.dumps(recorded, default=str)
    for secret in (CREDENTIAL, "Authorization", "API_TOKEN", "mcp.example.test"):
        assert secret not in serialized, secret
    assert mcp.calls[0]["arguments"] == {"title": SENTINEL}


def test_the_call_is_sent_with_the_arguments_that_were_proposed(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The recorded intent is plain JSON, and the backend sends the recorded arguments, not live objects."""
    seed_server(factory)

    result = node_run(factory).execute('{"tags": ["a", "b"], "n": 1}')

    assert result.status == "completed", result.error
    assert mcp.calls[0]["arguments"] == {"tags": ["a", "b"], "n": 1}
    with factory() as session:
        recorded = dispatcher_module.read_proposal(session, only_proposal(factory).proposal_id)
    assert recorded["args"]["arguments"] == {"tags": ["a", "b"], "n": 1}


@pytest.mark.parametrize("kind", [tool_run, node_run], ids=["tool-node", "mcp-node"])
def test_a_read_tool_runs_inline_with_no_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp, kind: Callable[..., McpRun]
) -> None:
    seed_server(factory)
    run = kind(factory, "lookup")
    mcp.result = {"hits": ["h1"]}

    result = run.execute('{"query": "refunds"}')

    assert result.status == "completed", result.error
    assert mcp.calls == [{"name": "lookup", "arguments": {"query": "refunds"}}]
    assert run.authorized == []
    assert proposals(factory) == []
    assert "h1" in result.output


def test_an_external_action_tool_is_recorded_as_irreversible_and_dispatched(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory, tools={"send_email": server_policy("external_action")})

    result = node_run(factory, "send_email").execute()

    assert result.status == "completed", result.error
    assert only_proposal(factory).effect_class == "irreversible_write"
    assert len(mcp.calls) == 1


def test_an_approval_requiring_read_keeps_its_binding_gate_and_runs_inline(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """``requires_approval`` is not an effect class: a read stays inline (the binding's own gate is separate)."""
    seed_server(factory, tools={"lookup": server_policy("read", approval=True)})

    result = node_run(factory, "lookup").execute()

    assert result.status == "completed", result.error
    assert proposals(factory) == []
    assert len(mcp.calls) == 1


# ---------------------------------------------------------------- trusted classification


@pytest.mark.parametrize("kind", [tool_run, node_run], ids=["tool-node", "mcp-node"])
def test_a_tool_the_server_lists_but_nobody_classified_is_refused_and_never_called(
    factory: sessionmaker[Session], mcp: FakeMcp, kind: Callable[..., McpRun]
) -> None:
    seed_server(factory, tools={"mystery": None})
    run = kind(factory, "mystery")

    result = run.execute()

    assert result.status == "error"
    assert "refused: mcp_tool_unclassified" in (result.error or "")
    assert mcp.calls == []
    assert run.authorized == []
    assert proposals(factory) == []


def test_a_server_claiming_a_tool_is_read_only_does_not_make_a_write_tool_inline(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The discovered entry carries ``readOnlyHint: true`` (the seed does that for every tool); the policy says write."""
    seed_server(factory)
    row = load_server_config(SERVER_ID)
    assert all(tool["annotations"] == {"readOnlyHint": True} for tool in row.discovered_tools)
    run = tool_run(factory)

    result = run.execute()

    assert result.status == "completed", result.error
    assert len(run.authorized) == 1, "the tool was proposed, not run inline on the server's word"
    assert only_proposal(factory).state == "committed"


def test_a_server_claiming_a_tool_is_destructive_does_not_stop_an_administrators_read(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The administrator's classification is the word of record in both directions, not the server's description."""
    seed_server(factory)
    with factory() as session:
        row = session.get(CaliberMcpServer, SERVER_ID)
        assert row is not None
        row.discovered_tools = [
            {"name": "lookup", "annotations": {"destructiveHint": True, "readOnlyHint": False}},
            {"name": "issue_write"},
        ]
        session.commit()
    run = tool_run(factory, "lookup")

    assert run.execute().status == "completed"
    assert run.authorized == []


def test_the_binding_level_is_not_the_classification(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """A binding that claims ``read`` cannot make an administrator's write inline; one claiming ``write`` does not
    override an administrator's read either."""
    seed_server(factory)
    claims_read = McpRun(factory, tool_node_manifest("issue_write", level="read"))
    assert claims_read.execute().status == "completed"
    assert len(claims_read.authorized) == 1

    claims_write = McpRun(factory, tool_node_manifest("lookup", level="write"))
    claims_write.execute("second")
    assert claims_write.authorized == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"discovered": ["lookup"]}, "mcp_tool_not_discovered"),
        ({"tools": {"issue_write": server_policy("write", allowed=False)}}, "mcp_tool_not_allowed"),
        ({"status": "disabled"}, "mcp_server_not_ready"),
        ({"discovered": []}, "mcp_tools_not_discovered"),
        (
            {"tools": {"issue_write": {"allowed": True, "side_effect_level": "write"}}},
            "mcp_tool_unclassified",
        ),
        (
            {"tools": {"issue_write": server_policy("danger")}},
            "mcp_tool_unclassified",
        ),
        ({"auth_type": "oauth"}, "mcp_server_not_ready"),
    ],
    ids=[
        "not-discovered",
        "blocked",
        "server-disabled",
        "nothing-discovered",
        "incomplete-policy",
        "invalid-level",
        "unsupported-auth",
    ],
)
def test_every_refusal_of_the_policy_gate_is_a_typed_code_with_no_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp, setup: dict[str, Any], code: str
) -> None:
    seed_server(factory, **setup)
    run = node_run(factory)

    result = run.execute()

    assert result.status == "error"
    assert f"MCP tool 'issue_write' refused: {code}" in (result.error or "")
    assert mcp.calls == [] and run.authorized == [] and proposals(factory) == []


def test_a_server_that_does_not_exist_is_refused_before_a_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    run = tool_run(factory)

    result = run.execute()

    assert result.status == "error"
    assert "refused: mcp_server_unavailable" in (result.error or "")
    assert mcp.calls == [] and proposals(factory) == []


def test_a_server_the_run_cannot_see_is_refused_before_a_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is read through the run's identity: a server that identity cannot see is not a server."""
    seed_server(factory)
    seen: list[Any] = []

    def invisible(server_id: str, *, identity: Any = None) -> Any:
        seen.append(identity)
        raise gateway_module.McpGatewayConfigError(f"MCP server {server_id!r} not found")

    monkeypatch.setattr(runtime_module, "load_server_config", invisible)
    run = node_run(factory)
    run.plan.mcp_identity = object()  # type: ignore[assignment]

    result = run.execute()

    assert result.status == "error"
    assert "MCP tool 'issue_write' refused: mcp_server_unavailable" in (result.error or "")
    assert seen == [run.plan.mcp_identity]
    assert mcp.calls == [] and proposals(factory) == []


# ---------------------------------------------------------------- the first-party database server (EP-114)


def first_party_server(factory: sessionmaker[Session], **tools: dict[str, Any]) -> None:
    seed_server(
        factory,
        transport="stdio",
        uri="",
        command=sys.executable,
        args=["-m", "caliber.mcp_servers.db", "--mode", "relational"],
        tools=tools,
        discovered=list(tools),
    )


def test_a_first_party_database_write_tool_is_refused_whatever_the_administrator_classified(
    factory: sessionmaker[Session], mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", sys.executable)
    first_party_server(
        factory,
        insert_rows=server_policy("read"),
        execute_sql=server_policy("external_action"),
    )

    for tool in ("insert_rows", "execute_sql"):
        run = node_run(factory, tool)
        result = run.execute()
        assert result.status == "error", tool
        assert "unsupported_effect_path" in (result.error or ""), tool
        assert run.authorized == []

    assert proposals(factory) == [] and mcp.calls == []


def test_a_first_party_database_read_tool_runs_inline_when_the_administrator_says_read(
    factory: sessionmaker[Session], mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", sys.executable)
    first_party_server(factory, run_query=server_policy("read"))
    seen: list[str] = []
    monkeypatch.setattr(
        gateway_module,
        "invoke_tool_raw_sync",
        lambda _server, *, tool_name, **_k: seen.append(tool_name) or {"rows": []},
    )

    result = node_run(factory, "run_query").execute('{"sql": "select 1"}')

    assert result.status == "completed", result.error
    assert seen == ["run_query"]
    assert proposals(factory) == []


def test_a_third_party_server_tool_named_like_a_database_write_is_not_refused_by_name(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The name is not an identity: a remote server's ``insert_rows`` is classified by its own administrator's policy."""
    seed_server(factory, tools={"insert_rows": server_policy("write")})

    run = node_run(factory, "insert_rows")
    assert run.execute().status == "completed"
    assert len(run.authorized) == 1

    seed_server(
        factory,
        server_id="MCP-other",
        name="other",
        tools={"insert_rows": None},
        discovered=["insert_rows"],
    )
    other = McpRun(factory, mcp_node_manifest("insert_rows", server_id="MCP-other"))
    refused = other.execute()
    assert "mcp_tool_unclassified" in (refused.error or "")


# ---------------------------------------------------------------- plans without a governed context


def test_a_protected_call_without_a_governed_context_is_refused_before_it_is_sent(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    result = tool_run_with(factory, context=False)

    assert result.status == "error"
    assert "requires a governed effect context to run protected MCP tool" in (result.error or "")
    assert mcp.calls == [] and proposals(factory) == []


def tool_run_with(factory: sessionmaker[Session], **kwargs: Any) -> Any:
    seed_server(factory)
    return tool_run(factory, **kwargs).execute()


def test_a_read_call_without_a_governed_context_still_runs(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    result = node_run(factory, "lookup", context=False).execute()

    assert result.status == "completed", result.error
    assert len(mcp.calls) == 1


def test_a_plan_wired_to_neither_the_registry_nor_an_authority_still_cannot_run_a_protected_tool(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """A caller-built plan keeps the direct call, but the gateway entry point itself refuses a protected tool."""
    seed_server(factory)
    run = node_run(factory, context=False, lookup=False)

    result = run.execute()

    assert result.status == "error"
    assert "refused for 'MCP-fake-1'/'issue_write': mcp_effect_requires_proposal" in (
        result.error or ""
    )
    assert mcp.calls == [] and proposals(factory) == []


def test_an_ungoverned_plan_keeps_calling_a_read_tool_directly(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    result = tool_run(factory, "lookup", context=False, lookup=False).execute()

    assert result.status == "completed", result.error
    assert len(mcp.calls) == 1


def test_a_model_chosen_protected_mcp_call_cannot_reach_the_server(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The bound callable a model's tool loop calls runs only reads: a protected call is refused, not sent (B04.6 proposes it)."""
    seed_server(factory)
    binding = tool_run(factory).plan.ir.nodes["tool_node"].binding  # type: ignore[attr-defined]
    bound = runtime_module._bind_mcp_tool(binding)

    with pytest.raises(runtime_module.ToolExecutionError) as refused:
        bound({"title": "x"})

    assert "refused on server 'MCP-fake-1': mcp_effect_requires_proposal" in str(refused.value)
    assert mcp.calls == []
    reading = runtime_module._bind_mcp_tool(
        tool_run(factory, "lookup").plan.ir.nodes["tool_node"].binding  # type: ignore[attr-defined]
    )
    assert reading({"query": "q"})["result"] == {"ok": True}


def test_a_preview_never_reaches_the_server(factory: sessionmaker[Session], mcp: FakeMcp) -> None:
    """A preview stubs a write binding and never proposes; a binding that under-claims read still cannot send."""
    seed_server(factory)

    stubbed = McpRun(factory, tool_node_manifest("issue_write", level="write"), preview=True)
    result = stubbed.execute()
    assert result.status == "completed", result.error
    assert mcp.calls == [] and proposals(factory) == [] and stubbed.authorized == []

    tool_run(factory, preview=True).execute()
    assert mcp.calls == [] and proposals(factory) == []


def test_a_protected_call_replaced_by_an_injected_callable_is_refused(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    run = tool_run(factory)

    result = run.execute(extra_tools={"lookup_docs": lambda *_a, **_k: {"x": 1}})

    assert result.status == "error"
    assert "is replaced by an injected callable and cannot be governed" in (result.error or "")
    assert mcp.calls == [] and proposals(factory) == []


def test_an_incomplete_governed_context_is_refused_before_a_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    run = tool_run(factory)
    assert run.plan.governed_effects is not None
    run.plan.governed_effects = GovernedEffectContext(
        session_factory=factory,
        workflow_run_id=RUN_ID,
        authorize=None,  # type: ignore[arg-type]
        receipt_verifier=verifier,
    )

    result = run.execute()

    assert "governed MCP effect context is incomplete" in (result.error or "")
    assert mcp.calls == [] and proposals(factory) == []


@pytest.mark.parametrize(
    "timeout", [0, -1, float("nan"), float("inf"), True, "30", 301, 10**400], ids=repr
)
def test_a_timeout_the_connector_does_not_accept_is_refused_before_a_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp, timeout: Any
) -> None:
    seed_server(factory)
    run = node_run(factory)
    node = run.plan.ir.nodes["tool_node"]
    node.timeout_seconds = timeout  # type: ignore[attr-defined]

    result = run.execute()

    assert result.status == "error"
    assert "refused: mcp_timeout_invalid" in (result.error or "")
    assert mcp.calls == [] and proposals(factory) == []


# ---------------------------------------------------------------- authority: approval, lease, replay


def test_a_write_whose_proposal_gets_no_authorization_never_reaches_the_server(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The issue's oracle: no approved proposal, no effect, and the dispatcher refuses the proposal it was left."""
    seed_server(factory)
    run = tool_run(factory, authorize=lambda _proposal: None)

    result = run.execute()

    assert result.status == "error"
    assert "authorization was not granted" in (result.error or "")
    recorded = only_proposal(factory)
    assert recorded.state == "proposed"
    with factory() as session:
        lease = leased(session, "runtime-owner")
        session.commit()
    call = mcp_tool.McpToolCall(SERVER_ID, "issue_write", lambda: load_server_config(SERVER_ID))
    with pytest.raises(EffectRefused) as refused:
        dispatch(
            factory,
            recorded.proposal_id,
            approval_id=None,
            lease=lease,
            backend_kind="mcp",
            backend_config={"call": call, "receipt_verifier": verifier},
            approval_validator=runtime_module.approval_valid_at_commit,
        )
    assert refused.value.code == "proposal_not_approved"
    assert mcp.calls == []


def test_an_authorizer_that_raises_is_reported_without_its_message(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    def boom(_proposal: Any) -> EffectAuthorization:
        raise RuntimeError(SENTINEL)

    result = tool_run(factory, authorize=boom).execute()

    assert "authorization failed: RuntimeError" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert mcp.calls == []


def test_a_stale_lease_never_reaches_the_server(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    def authorize(proposal: Any) -> EffectAuthorization:
        stale = grant(factory, proposal.proposal_id, owner="old-owner")
        with factory() as session:
            leased(session, "new-owner")  # a takeover: the lease captured above is stale
            session.commit()
        return stale

    result = tool_run(factory, authorize=authorize).execute()

    assert result.status == "error"
    assert "dispatch refused" in (result.error or "")
    assert mcp.calls == []
    assert only_proposal(factory).state == "proposed"


def test_replaying_a_finished_node_does_not_send_again(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    run = tool_run(factory)
    assert run.execute().status == "completed"

    again = run.execute()

    assert again.status == "error"
    assert "was already recorded as committed; not run again" in (again.error or "")
    assert len(mcp.calls) == 1


def test_two_executors_of_one_node_send_once(
    each_dialect: sessionmaker[Session], mcp: FakeMcp, request: pytest.FixtureRequest
) -> None:
    if request.node.callspec.id == "sqlite":
        pytest.skip("the claim lock is a PostgreSQL row lock; SQLite serializes writers anyway")
    factory = each_dialect
    seed_server(factory)
    barrier = threading.Barrier(2, timeout=30)
    shared: dict[str, EffectAuthorization] = {}
    guard = threading.Lock()
    outcomes: list[Any] = []

    def authorize(proposal: Any) -> EffectAuthorization:
        with guard:
            if "approval" not in shared:
                shared["approval"] = grant(factory, proposal.proposal_id)
        barrier.wait()
        return shared["approval"]

    def one() -> None:
        outcomes.append(tool_run(factory, authorize=authorize).execute())

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == 2
    assert sorted(result.status for result in outcomes) == ["completed", "error"]
    assert len(mcp.calls) == 1
    assert only_proposal(factory).state == "committed"


# ---------------------------------------------------------------- the server changed after the approval was requested


def test_a_server_reclassified_after_the_proposal_is_not_sent_the_call(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The record is read again when the backend is about to send: an administrator's later change is seen, not ignored."""
    seed_server(factory)

    def reclassify_then_grant(proposal: Any) -> EffectAuthorization:
        set_policy(factory, "issue_write", server_policy("read"))
        return grant(factory, proposal.proposal_id)

    result = tool_run(factory, authorize=reclassify_then_grant).execute()

    assert result.status == "error"
    assert mcp.calls == []
    proposal = only_proposal(factory)
    assert proposal.state == "failed", "proven not sent: nothing reached the server"


@pytest.mark.parametrize(
    "change",
    [
        lambda row: setattr(row, "uri", "https://mcp.example.test/other"),
        lambda row: setattr(row, "headers", {"X-New": "1"}),
        lambda row: setattr(row, "env", {"NEW": "1"}),
        lambda row: setattr(row, "auth_type", "token"),
        lambda row: setattr(row, "tool_policies", {}),
        lambda row: setattr(row, "status", "disabled"),
    ],
    ids=["uri", "headers", "env", "auth", "policy-removed", "disabled"],
)
def test_a_changed_server_record_is_never_sent_the_approved_call(
    factory: sessionmaker[Session], mcp: FakeMcp, change: Callable[[Any], None]
) -> None:
    seed_server(factory)

    def alter_then_grant(proposal: Any) -> EffectAuthorization:
        with factory() as session:
            row = session.get(CaliberMcpServer, SERVER_ID)
            assert row is not None
            change(row)
            session.commit()
        return grant(factory, proposal.proposal_id)

    result = tool_run(factory, authorize=alter_then_grant).execute()

    assert result.status == "error"
    assert mcp.calls == []
    assert only_proposal(factory).state == "failed"


def test_a_credential_value_change_is_not_part_of_the_approved_identity(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """Header and environment *values* are operator credentials and never part of the digest: rotating one keeps the approval."""
    seed_server(factory)

    def rotate_then_grant(proposal: Any) -> EffectAuthorization:
        with factory() as session:
            row = session.get(CaliberMcpServer, SERVER_ID)
            assert row is not None
            row.headers = {"Authorization": "Bearer rotated-value"}
            session.commit()
        return grant(factory, proposal.proposal_id)

    assert tool_run(factory, authorize=rotate_then_grant).execute().status == "completed"
    assert len(mcp.calls) == 1


# ---------------------------------------------------------------- what an MCP server can and cannot confirm


def test_a_server_that_fails_after_the_call_left_leaves_the_effect_unknown_and_is_not_resent(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    mcp.call_error = TimeoutError(SENTINEL)
    run = tool_run(factory)

    result = run.execute()

    assert result.status == "error"
    assert "did not commit: ambiguous" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert len(mcp.calls) == 1
    proposal = only_proposal(factory)
    assert proposal.state == "unknown"

    mcp.call_error = None
    again = run.execute()
    assert "was already recorded as unknown; not run again" in (again.error or "")
    assert len(mcp.calls) == 1, "an ambiguous MCP effect is never sent a second time"


def test_a_failure_the_server_reports_is_a_claim_not_proof_that_nothing_happened(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """``is_error`` is the untrusted server's word: the effect may have happened, so the proposal is ``unknown``, not ``failed``."""
    seed_server(factory)
    mcp.reports_error = True

    result = tool_run(factory).execute()

    assert result.status == "error"
    assert "did not commit: ambiguous:may_have_been_sent" in (result.error or "")
    assert only_proposal(factory).state == "unknown"
    assert len(mcp.calls) == 1


def test_a_session_that_never_opened_is_proven_not_sent_and_failed(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    mcp.initialize_error = ConnectionError("refused")

    result = tool_run(factory).execute()

    assert result.status == "error"
    assert "did not commit: not_sent" in (result.error or "")
    assert mcp.calls == []
    assert only_proposal(factory).state == "failed"


def test_a_rate_limited_dispatch_is_proven_not_sent_and_the_server_is_not_called(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The process-local limit is consumed by the call that really goes out; over it, nothing is sent (``failed``)."""
    seed_server(
        factory, tools={"issue_write": {**server_policy("write"), "rate_limit_per_minute": 1}}
    )
    server = load_server_config(SERVER_ID)
    gateway_module.invoke_tool_raw_sync(server, tool_name="issue_write", arguments={})
    assert len(mcp.calls) == 1

    result = tool_run(factory).execute()

    assert result.status == "error"
    assert "did not commit: not_sent" in (result.error or "")
    assert len(mcp.calls) == 1, "the limited dispatch never reached the server"
    assert only_proposal(factory).state == "failed"


def test_a_receipt_that_cannot_be_verified_does_not_become_committed_and_is_not_resent(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """Most MCP servers cannot confirm their own effects: without a verifier answer the call stays ``unknown``."""
    seed_server(factory)
    run = tool_run(factory, receipt_verifier=lambda _request, _response: None)

    result = run.execute()

    assert result.status == "error"
    assert "did not commit: receipt_unverified" in (result.error or "")
    assert only_proposal(factory).state == "unknown"
    assert count(factory, CaliberEffectReceipt) == 0
    assert run.execute().status == "error"
    assert len(mcp.calls) == 1


def test_a_verifier_that_raises_leaves_the_effect_unknown_and_it_is_not_resent(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    def boom(_request: Any, _response: dict[str, Any]) -> ReceiptFacts:
        raise RuntimeError(SENTINEL)

    run = tool_run(factory, receipt_verifier=boom)

    result = run.execute()

    assert result.status == "error"
    assert only_proposal(factory).state == "unknown"
    assert count(factory, CaliberEffectReceipt) == 0
    assert run.execute().status == "error"
    assert len(mcp.calls) == 1


def test_a_verifier_that_returns_something_else_than_receipt_facts_is_not_committed(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    run = tool_run(factory, receipt_verifier=lambda _request, _response: {"ok": True})

    result = run.execute()

    assert result.status == "error"
    assert only_proposal(factory).state == "unknown"
    assert count(factory, CaliberEffectReceipt) == 0


def test_the_verifier_sees_the_bounded_result_and_the_recorded_proposal(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    mcp.result = {"issue": "I-9"}
    seen: dict[str, Any] = {}

    def spying(request: Any, response: dict[str, Any]) -> ReceiptFacts:
        seen["response"] = response
        seen["resource"] = request.view.resource
        return verifier(request, response)

    assert tool_run(factory, receipt_verifier=spying).execute().status == "completed"

    assert seen == {
        "response": {"result": {"issue": "I-9"}},
        "resource": f"mcp/{SERVER_ID}/issue_write",
    }


@pytest.mark.parametrize(
    "result",
    [
        {"blob": "x" * (mcp_tool.MAX_RESULT_BYTES + 1)},
        {"a": float("nan")},
        {"deep": 1},
    ],
    ids=["too-big", "not-json", "too-deep"],
)
def test_a_result_that_is_not_bounded_plain_json_is_unknown_and_never_returned(
    factory: sessionmaker[Session], mcp: FakeMcp, result: dict[str, Any]
) -> None:
    seed_server(factory)
    if "deep" in result:
        nested: Any = 1
        for _ in range(mcp_tool.MAX_RESULT_DEPTH + 1):
            nested = {"n": nested}
        result = nested
    mcp.result = result

    outcome = tool_run(factory).execute()

    assert outcome.status == "error"
    assert "did not commit: ambiguous:result_out_of_bounds" in (outcome.error or "")
    assert only_proposal(factory).state == "unknown"
    assert len(mcp.calls) == 1


def test_a_result_that_tries_to_instruct_the_platform_is_only_returned_as_data(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    mcp.result = {"side_effect_level": "read", "requires_approval": False, "approved": True}

    result = tool_run(factory).execute()

    assert result.status == "completed", result.error
    proposal = only_proposal(factory)
    assert proposal.state == "committed" and proposal.effect_class == "irreversible_write"


# ---------------------------------------------------------------- no payload or credential in what is shown


def test_a_refusal_never_carries_the_arguments_or_a_credential(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory, tools={"mystery": None})

    result = node_run(factory, "mystery").execute(json.dumps({"title": SENTINEL}))

    assert result.status == "error"
    assert SENTINEL not in (result.error or "")
    assert CREDENTIAL not in (result.error or "")


def test_a_dispatch_failure_message_names_only_the_code(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    mcp.call_error = RuntimeError(f"{SENTINEL} {CREDENTIAL}")

    result = tool_run(factory).execute(json.dumps({"title": SENTINEL}))

    assert SENTINEL not in (result.error or "") and CREDENTIAL not in (result.error or "")
    assert only_proposal(factory).state == "unknown"


# ---------------------------------------------------------------- the connector and its admission


def test_the_mcp_connector_is_admitted_and_only_for_its_own_settings(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    admitted = dispatcher_module.admit_backend("mcp")
    assert (admitted.connector, admitted.connector_version) == ("mcp-tool", "1")
    assert set(admitted.accepted_risks) == {"write_not_exactly_once", "key_retention_unconfirmed"}
    with factory() as session, pytest.raises(EffectRefused) as refused:
        submit_proposal(
            session,
            workflow_run_id=RUN_ID,
            node_id="n",
            occurrence_id="n",
            connector="mcp-tool",
            connector_version="1",
            resource=f"mcp/{SERVER_ID}/issue_write",
            args={"tool": "issue_write"},
        )
    assert refused.value.code == "connector_not_admitted"

    seed_server(factory)
    run = tool_run(factory, authorize=lambda _proposal: None)
    run.execute()
    (proposal_id,) = run.authorized
    with factory() as session:
        lease = leased(session, "runtime-owner")
        session.commit()
    for config in (
        {"call": "not a call", "receipt_verifier": verifier},
        {"receipt_verifier": verifier},
        {
            "call": mcp_tool.McpToolCall(SERVER_ID, "t", lambda: None),
            "receipt_verifier": verifier,
            "x": 1,
        },
    ):
        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id="rap-x",
                lease=lease,
                backend_kind="mcp",
                backend_config=config,
                approval_validator=runtime_module.approval_valid_at_commit,
            )
        assert refused.value.code == "connector_config_not_admitted"
    assert mcp.calls == []


def test_a_registry_tool_cannot_be_named_like_the_mcp_connector() -> None:
    from caliber.profile import reserved_connector_names

    assert {"mcp", "mcp-tool"} <= reserved_connector_names()


def _recorded_request(factory: sessionmaker[Session], **changes: Any) -> Any:
    """The backend's request for the proposal a real run recorded, optionally with fields of its intent altered."""
    seed_server(factory)
    run = tool_run(factory, authorize=lambda _proposal: None)
    run.execute('{"title": "t"}')
    (proposal_id,) = run.authorized
    with factory() as session:
        view = dispatcher_module.proposal_view(session, proposal_id)
        intent = dispatcher_module.read_proposal(session, proposal_id)
    args = {**intent["args"], **changes.pop("args", {})}
    return dispatcher_module.EffectRequest(
        view=view if not changes else _replace_view(view, **changes),
        intent={**intent, "args": args},
    )


def _replace_view(view: Any, **changes: Any) -> Any:
    from dataclasses import replace

    return replace(view, **changes)


def _call(**overrides: Any) -> mcp_tool.McpToolCall:
    fields: dict[str, Any] = {
        "server_id": SERVER_ID,
        "tool_name": "issue_write",
        "load_server": lambda: load_server_config(SERVER_ID),
    }
    fields.update(overrides)
    return mcp_tool.McpToolCall(**fields)


@pytest.mark.parametrize(
    "tamper",
    [
        {"args": {"tool": "lookup"}},
        {"args": {"server_id": "MCP-other"}},
        {"args": {"server_digest": "sha256:" + "0" * 64}},
        {"args": {"extra": 1}},
        {"args": {"arguments": "not an object"}},
        {"resource": "mcp/MCP-fake-1/lookup"},
        {"connector": "http"},
        {"connector_version": "2"},
    ],
    ids=[
        "tool",
        "server",
        "digest",
        "extra-field",
        "arguments-shape",
        "resource",
        "connector",
        "version",
    ],
)
def test_the_backend_sends_nothing_for_a_proposal_that_is_not_its_trusted_call(
    factory: sessionmaker[Session], mcp: FakeMcp, tamper: dict[str, Any]
) -> None:
    request = _recorded_request(factory, **tamper)
    backend = BACKENDS["mcp"].backend

    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        backend.perform(request, {"call": _call(), "receipt_verifier": verifier})

    assert refused.value.never_sent is True
    assert mcp.calls == []


def test_the_backend_refuses_when_its_own_settings_are_missing(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    request = _recorded_request(factory)
    backend = BACKENDS["mcp"].backend

    for config in (None, {}, {"call": _call()}, {"receipt_verifier": verifier}):
        with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
            backend.perform(request, config)
        assert refused.value.code == "backend_config_missing"
    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        backend.perform(
            dispatcher_module.EffectRequest(view=request.view, intent={"args": "nope"}),
            {"call": _call(), "receipt_verifier": verifier},
        )
    assert refused.value.code == "connector_intent_unsupported"
    assert mcp.calls == []


def test_the_backend_refuses_a_server_it_cannot_read_and_sends_nothing(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    request = _recorded_request(factory)

    def unreadable() -> Any:
        raise RuntimeError(SENTINEL)

    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        BACKENDS["mcp"].backend.perform(
            request, {"call": _call(load_server=unreadable), "receipt_verifier": verifier}
        )
    assert refused.value.code == "mcp_server_unavailable"
    assert SENTINEL not in str(refused.value)

    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        BACKENDS["mcp"].backend.perform(
            request,
            {
                "call": _call(
                    load_server=lambda: load_server_config(
                        SERVER_ID
                    ).__class__(  # a record of another id
                        **{**load_server_config(SERVER_ID).__dict__, "server_id": "MCP-other"}
                    )
                ),
                "receipt_verifier": verifier,
            },
        )
    assert refused.value.code == "mcp_server_unavailable"
    assert mcp.calls == []


def test_a_gateway_error_the_backend_cannot_classify_is_ambiguous(
    factory: sessionmaker[Session], mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _recorded_request(factory)

    def raises(*_a: Any, **_k: Any) -> Any:
        raise ValueError("a bug in some transport")

    monkeypatch.setattr(gateway_module, "invoke_tool_raw_sync", raises)

    transport = BACKENDS["mcp"].backend.perform(
        request, {"call": _call(), "receipt_verifier": verifier}
    )

    assert transport.outcome.outcome.value == "ambiguous"
    assert transport.outcome.code == "unclassified_error"


def test_a_gateway_transport_error_that_does_not_say_is_ambiguous_too(
    factory: sessionmaker[Session], mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _recorded_request(factory)

    def raises(*_a: Any, **_k: Any) -> Any:
        raise McpGatewayTransportError("unknown")

    monkeypatch.setattr(gateway_module, "invoke_tool_raw_sync", raises)

    transport = BACKENDS["mcp"].backend.perform(
        request, {"call": _call(), "receipt_verifier": verifier}
    )

    assert (transport.outcome.outcome.value, transport.outcome.code) == (
        "ambiguous",
        "may_have_been_sent",
    )


# ---------------------------------------------------------------- the result is untrusted data


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth - 1):
        value = {"n": value}
    return value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "text",
        0,
        [1, 2, 3],
        {"a": {"b": [1, {"c": None}]}},
        (1, 2),
        {"unicode": "café ☃"},
        _nested(mcp_tool.MAX_RESULT_DEPTH),
        {"pad": "x" * (mcp_tool.MAX_RESULT_BYTES - len('{"pad":""}'))},
    ],
    ids=["none", "str", "int", "list", "nested", "tuple", "unicode", "max-depth", "max-bytes"],
)
def test_a_result_inside_the_bounds_is_returned_as_plain_json(value: Any) -> None:
    bounded = mcp_tool.bounded_result(value)

    assert bounded is not None
    assert bounded[0] == json.loads(json.dumps(value))


@pytest.mark.parametrize(
    "value",
    [
        _nested(mcp_tool.MAX_RESULT_DEPTH + 1),
        {"pad": "x" * (mcp_tool.MAX_RESULT_BYTES - len('{"pad":""}') + 1)},
        float("nan"),
        {"a": float("inf")},
        object(),
        {"a": {1, 2}},
        {(1, 2): "tuple key"},
    ],
    ids=["too-deep", "too-big", "nan", "inf", "object", "set", "tuple-key"],
)
def test_a_result_outside_the_bounds_or_not_json_is_refused(value: Any) -> None:
    assert mcp_tool.bounded_result(value) is None


def test_a_result_with_text_that_cannot_be_encoded_is_refused() -> None:
    # Built here, not as a parameter: a lone surrogate in a parameter id aborts the whole report run.
    unencodable = "lone surrogate " + chr(0xD800)

    assert mcp_tool.bounded_result({"a": unencodable}) is None


def test_a_self_referencing_result_is_refused_not_followed() -> None:
    loop: dict[str, Any] = {}
    loop["me"] = loop

    assert mcp_tool.bounded_result(loop) is None


def test_the_returned_result_shares_nothing_with_the_servers_objects() -> None:
    original = {"a": [1, 2]}

    bounded = mcp_tool.bounded_result(original)

    assert bounded is not None and bounded[0] == original and bounded[0] is not original
    assert bounded[0]["a"] is not original["a"]


# ---------------------------------------------------------------- prepare and check_recorded


def test_prepare_returns_the_proposal_material_for_a_protected_call_and_none_for_a_refusal(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory, tools={"mystery": None})

    ready = mcp_tool.prepare(_call(), {"title": "t"})
    refused = mcp_tool.prepare(_call(tool_name="mystery"), {})

    assert ready.route.route == "dispatch" and ready.resource == f"mcp/{SERVER_ID}/issue_write"
    assert ready.args is not None and ready.args["arguments"] == {"title": "t"}
    assert (refused.route.route, refused.route.code, refused.args) == (
        "refuse",
        "mcp_tool_unclassified",
        None,
    )
    assert mcp.calls == []


def test_prepare_refuses_a_loader_that_returns_another_server_or_fails(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)

    def explode() -> Any:
        raise RuntimeError(SENTINEL)

    for loader in (explode, lambda: None):
        with pytest.raises(mcp_tool.McpCallRefused) as refused:
            mcp_tool.prepare(_call(load_server=loader), {})
        assert refused.value.code == "mcp_server_unavailable"
        assert SENTINEL not in str(refused.value)


def test_check_recorded_returns_the_server_and_a_copy_of_the_recorded_arguments(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    prepared = mcp_tool.prepare(_call(), {"title": "t"})
    assert prepared.args is not None

    server, arguments = mcp_tool.check_recorded(
        _call(), resource=prepared.resource, args=prepared.args
    )

    assert server.server_id == SERVER_ID
    assert arguments == {"title": "t"} and arguments is not prepared.args["arguments"]


def test_check_recorded_refuses_a_call_that_is_no_longer_protected(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    seed_server(factory)
    prepared = mcp_tool.prepare(_call(), {})
    assert prepared.args is not None
    set_policy(factory, "issue_write", server_policy("read"))

    with pytest.raises(mcp_tool.McpCallRefused) as refused:
        mcp_tool.check_recorded(_call(), resource=prepared.resource, args=prepared.args)

    assert refused.value.code == mcp_tool.INTENT_MISMATCH


def test_the_capability_record_promises_nothing_about_keys_or_lookups() -> None:
    capability = mcp_tool.CAPABILITY

    assert capability.idempotency_key_header is None
    assert capability.lookup_by_key is False and capability.absence_proves_noncommit is False
    assert capability.carries_a_write_exactly_once is False
    assert capability.retention_confirmed is False
    assert capability.definitive_rejection_statuses == frozenset()


def test_the_pinned_digests_match_what_the_dispatcher_registers() -> None:
    from caliber.effects.admission import configuration_digest
    from caliber.profile import REFERENCE_CONNECTORS

    (record,) = [t for t in REFERENCE_CONNECTORS if t.backend_kind == "mcp"]
    registration = BACKENDS["mcp"]

    assert record.capability_digest == registration.capability.digest()  # type: ignore[union-attr]
    assert record.configuration_digest == configuration_digest(registration.configuration or {})
    assert record.implementation == "caliber.effects.dispatcher:_McpToolBackend"
    assert (record.connector, record.connector_version) == (mcp_tool.CONNECTOR, mcp_tool.VERSION)


def test_an_inline_mcp_node_passes_the_runs_identity_to_the_gateway(
    factory: sessionmaker[Session], mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The visibility-scoped identity of the run reaches the gateway on the inline path, as it did before B04.5."""
    seed_server(factory)
    seen: list[dict[str, Any]] = []

    def fake_invoke(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"text": "ok"}

    monkeypatch.setattr(runtime_module, "invoke_tool_by_server_id_sync", fake_invoke)
    run = node_run(factory, "lookup", context=False, lookup=False)
    identity = object()
    run.plan.mcp_identity = identity  # type: ignore[assignment]

    result = run.execute()

    assert result.status == "completed", result.error
    assert seen[0]["identity"] is identity and seen[0]["server_id"] == SERVER_ID


def test_the_backend_refuses_a_verifier_answer_that_is_not_receipt_facts(
    factory: sessionmaker[Session], mcp: FakeMcp
) -> None:
    """The check is the backend's own, before the settle: an answer that is not ``ReceiptFacts`` is never a receipt."""
    request = _recorded_request(factory)

    transport = BACKENDS["mcp"].backend.perform(
        request, {"call": _call(), "receipt_verifier": lambda _request, _response: {"ok": True}}
    )

    assert transport.verify is not None
    with pytest.raises(dispatcher_module._TransportRefusedError) as refused:
        transport.verify()
    assert refused.value.code == "receipt_verifier_invalid"
    assert len(mcp.calls) == 1
