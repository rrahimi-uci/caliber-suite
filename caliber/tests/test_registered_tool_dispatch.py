"""B04.3 (#741): registry tools reach the dispatcher through the real runtime executor.

A workflow tool node runs a registered Python tool inline only when its **registry row** says it is read-class.
Every other tool is proposed, approved and dispatched through the ``registered_tool`` backend under the execution-owner
lease; a refused built-in never runs. The registry row decides, never the manifest binding.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import caliber.effects.dispatcher as dispatcher_module
import caliber.workflows.runtime as runtime_module
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberEventOutbox,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
)
from caliber.effects.dispatcher import BACKENDS, EffectRefused, dispatch
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.registered_tools import (
    RegisteredToolIdentity,
    load_registered_tool_identity,
    route_registered_tool,
)
from caliber.ids import new_tool_id
from caliber.workflows.builtin_tools import (
    BUILTIN_TOOL_OWNER,
    BUILTIN_TOOL_VERSION,
    register_builtin_tools,
)
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.dialect_helpers import DIALECTS
from tests.effect_helpers import leased
from tests.test_effect_dispatcher import AT, EVIDENCE, POLICY, _validator
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database
from tests.workflow_helpers import make_manifest

RUN_ID = "RUN-ep"
SENTINEL = "PAYLOAD-SENTINEL-4f9c"
TOOL_MODULE = "registered_tool_fixture"
Authorizer = Callable[[Any], EffectAuthorization | None]


@contextmanager
def _world(
    dialect: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    """A migrated database with one running workflow run and the deterministic approval-validator seam."""
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", _validator)
    monkeypatch.setattr(runtime_module, "approval_valid_at_commit", _validator)
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        yield factory


@pytest.fixture
def factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        yield factory


@pytest.fixture(params=DIALECTS)
def each_dialect(
    request: pytest.FixtureRequest, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    with _world(request.param, tmp_path, monkeypatch) as factory:
        yield factory


def tool_entry(name: str, level: str, **overrides: Any) -> ToolRegistryEntry:
    fields: dict[str, Any] = {
        "name": name,
        "version": "1.0",
        "module_path": TOOL_MODULE,
        "callable_name": name,
        "side_effect_level": level,
    }
    fields.update(overrides)
    return ToolRegistryEntry(**fields)


def seed_row(factory: sessionmaker[Session], entry: ToolRegistryEntry, owner: str = "@ops") -> None:
    """The registry row the runtime reads for ``entry``: written from the entry, so they agree by default."""
    with factory() as session:
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name=entry.name,
                version=entry.version,
                module_path=entry.module_path,
                callable_name=entry.callable_name,
                side_effect_level=entry.side_effect_level,
                execution_backend=entry.execution_backend,
                owner=owner,
            )
        )
        session.commit()


class Tool:
    """A registered callable that counts how often it really ran, and with what."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = {"ticket": "T-1"} if result is None else result

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.result


def tool_manifest(tool_name: str, **tool_binding: Any) -> dict[str, Any]:
    data = make_manifest()
    del data["nodes"]["agent"]
    data["nodes"]["tool_node"] = {
        "id": "tool_node",
        "type": "tool",
        "tool_name": tool_name,
        "inputs": {"input": {"type": "string"}, "arguments": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["tools"] = {
        tool_name: {
            "registry_ref": f"tool.{tool_name}.v1",
            "version_constraint": ">=1.0,<2.0",
            **tool_binding,
        }
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_node", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_node", "to": "final", "map": {"text": "response"}},
    ]
    return data


def grant(
    factory: sessionmaker[Session],
    proposal_id: str,
    *,
    approval_id: str = "rap-dispatch",
    owner: str = "runtime-owner",
) -> EffectAuthorization:
    """An approved exact-intent request for ``proposal_id`` and the live execution-owner lease."""
    with factory() as session:
        proposal = session.get(CaliberEffectProposal, proposal_id)
        assert proposal is not None
        session.add(
            CaliberRuntimeApprovalRequest(
                runtime_approval_id=approval_id,
                workflow_run_id=proposal.workflow_run_id,
                project_id="PRJ-ep",
                node_id=proposal.node_id,
                status="approved",
                requested_at=AT.replace(tzinfo=None),
                decided_at=AT.replace(tzinfo=None),
                decided_by="approver",
                policy_snapshot={"required_role": "caliber.approver", "approval_count": 1},
                proposal_id=proposal_id,
                intent_hash=proposal.intent_hash,
                evidence_hash=EVIDENCE,
                policy_digest=POLICY,
                expected_resource_version=proposal.expected_resource_version,
                expires_at=AT.replace(tzinfo=None).replace(year=2099),
                requested_by="editor",
            )
        )
        lease = leased(session, owner)
        session.commit()
    return EffectAuthorization(
        approval_id=approval_id,
        approver_scopes=frozenset({"caliber.approver"}),
        lease=lease,
        evidence_hash=EVIDENCE,
    )


def verifier(request: Any, response: dict[str, Any]) -> ReceiptFacts:
    assert "result" in response
    return ReceiptFacts(
        attempt=request.view.attempt or 1,
        idempotency_key=request.view.logical_key,
        connector=request.view.connector,
        connector_version=request.view.connector_version,
        resource=request.view.resource,
        intent_hash=request.view.intent_hash,
        downstream_request_id="tool-downstream-1",
        response_digest="sha256:" + "d" * 64,
    )


class Run:
    """One plan over ``tool_node``; ``authorized`` records every proposal the runtime asked to authorize."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        entry: ToolRegistryEntry,
        tool: Tool,
        *,
        authorize: Authorizer | None = None,
        receipt_verifier: Callable[[Any, dict[str, Any]], Any] = verifier,
        context: bool = True,
        lookup: bool = True,
        workflow_version_id: str | None = "version-b04-3",
        binding: dict[str, Any] | None = None,
    ) -> None:
        self.factory = factory
        self.tool = tool
        self.authorized: list[str] = []
        user_authorize = authorize

        def authorize_and_record(proposal: Any) -> EffectAuthorization | None:
            self.authorized.append(proposal.proposal_id)
            if user_authorize is not None:
                return user_authorize(proposal)
            return grant(factory, proposal.proposal_id)

        resolver = InMemoryToolResolver([entry])
        resolver.register(entry, tool)
        manifest = parse_manifest(tool_manifest(entry.name, **(binding or {})))
        self.plan = RuntimePlan(
            ir=build_ir(manifest, resolver, version="1"),
            resolver=resolver,
            workflow_version_id=workflow_version_id,
            registered_tool_identity=(
                (lambda name, version: _read_identity(factory, name, version)) if lookup else None
            ),
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

    def execute(self, text: str = "open a ticket", **kwargs: Any) -> Any:
        return execute(self.plan, text, executor=FakeWorkflowExecutor(), **kwargs)


def _read_identity(
    factory: sessionmaker[Session], name: str, version: str
) -> RegisteredToolIdentity | None:
    with factory() as session:
        return load_registered_tool_identity(session, name=name, version=version)


def proposals(factory: sessionmaker[Session]) -> list[CaliberEffectProposal]:
    with factory() as session:
        return list(session.execute(select(CaliberEffectProposal)).scalars())


def count(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


# ---------------------------------------------------------------- governed write tools


def test_a_write_tool_runs_only_after_proposal_approval_and_dispatch(
    each_dialect: sessionmaker[Session],
) -> None:
    factory = each_dialect
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    seen_at_call: dict[str, Any] = {}

    class Recording(Tool):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            with factory() as session:
                row = session.execute(select(CaliberEffectProposal)).scalar_one()
                seen_at_call["state"] = row.state
                seen_at_call["consumed"] = session.execute(
                    select(CaliberRuntimeApprovalRequest.consumed_at)
                ).scalar_one()
                seen_at_call["outbox"] = session.execute(
                    select(func.count()).select_from(CaliberEventOutbox)
                ).scalar_one()
            return super().__call__(*args, **kwargs)

    tool = Recording()
    run = Run(factory, entry, tool)

    result = run.execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    assert tool.calls[0] == (("open a ticket",), {})
    assert "T-1" in result.output
    # The tool ran only after the claim committed: dispatched, approval consumed, outbox event written.
    assert seen_at_call["state"] == "dispatched"
    assert seen_at_call["consumed"] is not None
    assert seen_at_call["outbox"] == 1
    (proposal,) = proposals(factory)
    assert proposal.state == "committed"
    assert proposal.connector == "registered_tool"
    assert proposal.resource == "registered-tool/open_ticket/1.0"
    assert proposal.node_id == "tool_node"
    # An administrator-registered tool is not in the inventory: the recorded class is the fail-closed one.
    assert proposal.effect_class == "irreversible_write"
    assert count(factory, CaliberEffectReceipt) == 1


def test_the_tool_runs_with_the_arguments_that_were_proposed(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool)

    result = run.execute('{"summary": "disk full"}')

    assert result.status == "completed", result.error
    (proposal,) = proposals(factory)
    with factory() as session:
        recorded = dispatcher_module.read_proposal(session, proposal.proposal_id)
    assert recorded["args"] == {
        "tool": "open_ticket",
        "version": "1.0",
        "arguments": {"summary": "disk full"},
        "input": '{"summary": "disk full"}',
    }
    assert tool.calls == [((), {"summary": "disk full"})]


def test_the_tool_receives_the_recorded_arguments_not_the_callers_live_objects(
    factory: sessionmaker[Session],
) -> None:
    """The recorded intent is plain JSON: a tuple the caller holds is a list in the proposal, and the tool gets the list."""
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool)
    binding = run.plan.ir.nodes["tool_node"].binding  # type: ignore[attr-defined]
    live = {"tags": ("a", "b"), "n": 1}

    result = runtime_module._perform_registered_tool_effect(
        run.plan,
        node_id="tool_node",
        binding=binding,
        route=route_registered_tool(_read_identity(factory, "open_ticket", "1.0")),
        fn=tool,
        arguments=live,
        fallback_input="text",
    )

    assert result == {"ticket": "T-1"}
    assert tool.calls == [((), {"tags": ["a", "b"], "n": 1})]
    assert isinstance(tool.calls[0][1]["tags"], list)


def test_a_floor_makes_the_recorded_class_stricter_and_never_weaker(
    factory: sessionmaker[Session],
) -> None:
    from caliber.effects.dispatcher import submit_proposal

    def record(node: str, **kwargs: Any) -> str:
        with factory() as session:
            recorded = submit_proposal(
                session,
                workflow_run_id=RUN_ID,
                node_id=node,
                occurrence_id=node,
                connector="registered_tool",
                connector_version="1",
                resource=f"registered-tool/{node}/1.0",
                args={"tool": node},
                **kwargs,
            )
            session.commit()
            row = session.get(CaliberEffectProposal, recorded.proposal_id)
            assert row is not None
            return row.effect_class

    # a trusted built-in whose inventory class is ``read``
    assert record("n1", tool_name="list_workdir_files", caliber_defined=True) == "read"
    assert (
        record(
            "n2",
            tool_name="list_workdir_files",
            caliber_defined=True,
            min_effect_class="irreversible_write",
        )
        == "irreversible_write"
    )
    # a floor below the inventory's class does not weaken it
    assert (
        record("n3", tool_name="write_workdir_file", caliber_defined=True, min_effect_class="read")
        == "reversible_write"
    )
    with pytest.raises(EffectRefused) as refused:
        record("n4", tool_name="list_workdir_files", caliber_defined=True, min_effect_class="nope")
    assert refused.value.code == "invalid_effect_class"


def test_replaying_a_finished_node_does_not_run_the_tool_again(
    factory: sessionmaker[Session],
) -> None:
    """Re-executing a finished node reuses its proposal and does not call the tool again."""
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    first = Run(factory, entry, tool)
    assert first.execute().status == "completed"

    second = Run(factory, entry, tool)
    result = second.execute()

    assert result.status == "error"
    assert "was already recorded as committed; not run again" in (result.error or "")
    assert len(tool.calls) == 1
    assert len(proposals(factory)) == 1
    assert second.authorized == [], (
        "a finished effect is not authorized or dispatched a second time"
    )


def test_a_retry_after_an_unknown_effect_keeps_the_first_accurate_error(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)

    class Exploding(Tool):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            super().__call__(*args, **kwargs)
            raise RuntimeError("boom")

    tool = Exploding()
    first = Run(factory, entry, tool)
    assert "did not commit" in (first.execute().error or "")

    second = Run(factory, entry, tool)
    result = second.execute()

    assert result.status == "error"
    assert "was already recorded as unknown; not run again" in (result.error or "")
    assert "authorization failed" not in (result.error or "")
    assert second.authorized == []
    assert len(tool.calls) == 1


def test_an_external_action_tool_is_recorded_as_irreversible_and_dispatched(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("send_refund", "external_action")
    seed_row(factory, entry)
    tool = Tool()

    result = Run(factory, entry, tool).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    assert proposals(factory)[0].effect_class == "irreversible_write"


def test_a_tool_that_raises_leaves_the_effect_unknown_and_is_not_retried(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)

    class Exploding(Tool):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            super().__call__(*args, **kwargs)
            raise RuntimeError(f"upstream said {SENTINEL}")

    tool = Exploding()
    run = Run(factory, entry, tool, binding={"max_retries": 3})

    result = run.execute()

    assert result.status == "error"
    assert "registered tool effect did not commit" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert len(tool.calls) == 1, "a governed write is never retried, whatever the binding says"
    (proposal,) = proposals(factory)
    assert proposal.state == "unknown"


def test_a_tool_the_runtime_cannot_verify_does_not_become_committed(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool, receipt_verifier=lambda _request, _response: None)

    result = run.execute()

    assert result.status == "error"
    assert "did not commit" in (result.error or "")
    assert len(tool.calls) == 1
    assert proposals(factory)[0].state == "unknown"
    assert count(factory, CaliberEffectReceipt) == 0


def test_a_tool_with_an_invalid_receipt_verifier_result_is_not_committed(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    run = Run(factory, entry, Tool(), receipt_verifier=lambda _request, _response: "not facts")

    result = run.execute()

    assert result.status == "error"
    assert proposals(factory)[0].state != "committed"


# ---------------------------------------------------------------- refusals before the tool runs


def test_a_write_tool_without_a_governed_context_is_refused_before_it_runs(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()

    result = Run(factory, entry, tool, context=False).execute()

    assert result.status == "error"
    assert "requires a governed effect context" in (result.error or "")
    assert tool.calls == []
    assert proposals(factory) == []


def test_a_write_tool_whose_proposal_gets_no_authorization_never_runs(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool, authorize=lambda _proposal: None)

    result = run.execute()

    assert result.status == "error"
    assert "registered tool effect authorization was not granted" in (result.error or "")
    assert tool.calls == []
    (proposal,) = proposals(factory)
    assert proposal.state == "proposed"


@pytest.mark.parametrize("granted", [True, "approved", {"approval_id": "rap-dispatch"}, 1])
def test_only_an_exact_authorization_object_lets_a_protected_tool_run(
    factory: sessionmaker[Session], granted: Any
) -> None:
    """A provider that answers ``True`` (or anything but an ``EffectAuthorization``) is not an approval."""
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()

    result = Run(factory, entry, tool, authorize=lambda _proposal: granted).execute()

    assert result.status == "error"
    assert "authorization was not granted" in (result.error or "")
    assert tool.calls == []


def test_an_authorizer_that_raises_is_reported_without_its_message(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)

    def authorize(_proposal: Any) -> None:
        raise ValueError(f"approval store said {SENTINEL}")

    tool = Tool()
    result = Run(factory, entry, tool, authorize=authorize).execute()

    assert result.status == "error"
    assert "authorization failed: ValueError" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert tool.calls == []


def test_an_approval_for_another_proposal_never_runs_the_tool(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical validator refuses; nothing is consumed and the tool is not called."""
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)

    def refusing(*_args: Any, **_kwargs: Any) -> Any:
        raise dispatcher_module.ApprovalCommitRefused("approval_intent_mismatch", "not this intent")

    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", refusing)
    monkeypatch.setattr(runtime_module, "approval_valid_at_commit", refusing)
    tool = Tool()
    run = Run(factory, entry, tool)

    result = run.execute()

    assert result.status == "error"
    assert "dispatch refused" in (result.error or "")
    assert tool.calls == []
    with factory() as session:
        assert (
            session.execute(select(CaliberRuntimeApprovalRequest.consumed_at)).scalar_one() is None
        )


def test_a_stale_lease_never_runs_the_tool(factory: sessionmaker[Session]) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)

    def authorize(proposal: Any) -> EffectAuthorization:
        stale = grant(factory, proposal.proposal_id, owner="old-owner")
        with factory() as session:
            leased(session, "new-owner")  # a takeover: the lease captured above is stale
            session.commit()
        return stale

    tool = Tool()
    result = Run(factory, entry, tool, authorize=authorize).execute()

    assert result.status == "error"
    assert "dispatch refused" in (result.error or "")
    assert tool.calls == []


def test_a_malformed_proposal_is_refused_with_a_code_and_no_payload(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    huge = SENTINEL + "x" * (300 * 1024)

    result = Run(factory, entry, tool).execute(huge)

    assert result.status == "error"
    assert "registered tool effect proposal refused" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert tool.calls == []
    assert proposals(factory) == []


def test_a_non_finite_argument_is_refused_before_anything_is_recorded(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool)

    result = run.execute('{"amount": 1e999}')

    assert result.status == "error"
    assert "proposal refused" in (result.error or "")
    assert tool.calls == []
    assert proposals(factory) == []


def test_a_context_without_authority_is_incomplete(factory: sessionmaker[Session]) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    run = Run(factory, entry, Tool())
    assert run.plan.governed_effects is not None
    run.plan.governed_effects = replace(run.plan.governed_effects, authorize="not callable")  # type: ignore[arg-type]

    result = run.execute()

    assert result.status == "error"
    assert "governed registered-tool effect context is incomplete" in (result.error or "")
    assert run.tool.calls == []


# ---------------------------------------------------------------- the registry row decides


def test_a_read_tool_runs_inline_and_never_touches_the_dispatcher(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("lookup_policy", "read")
    seed_row(factory, entry)
    tool = Tool(result={"policy": "30-day refund"})
    run = Run(factory, entry, tool)

    result = run.execute("refund policy")

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    assert run.authorized == []
    assert proposals(factory) == []


def test_a_binding_that_claims_read_cannot_downgrade_a_write_row(
    factory: sessionmaker[Session],
) -> None:
    """The resolver entry (what a manifest or export says) claims ``read``; the registry row says ``write``."""
    entry = tool_entry("open_ticket", "read")
    seed_row(factory, replace(entry, side_effect_level="write"))
    tool = Tool()
    run = Run(factory, entry, tool, context=False)
    assert run.plan.ir.nodes["tool_node"].binding.side_effect_level == "read"  # type: ignore[attr-defined]

    result = run.execute()

    assert result.status == "error"
    assert "requires a governed effect context" in (result.error or "")
    assert tool.calls == []


def test_a_name_that_is_not_in_the_registry_is_unverified_and_fails_closed(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "read")  # no row is seeded
    tool = Tool()

    refused = Run(factory, entry, tool, context=False).execute()
    assert refused.status == "error"
    assert "requires a governed effect context" in (refused.error or "")
    assert tool.calls == []

    governed = Run(factory, entry, tool).execute()
    assert governed.status == "completed", governed.error
    assert len(tool.calls) == 1
    assert proposals(factory)[0].effect_class == "irreversible_write"


def test_a_plan_wired_to_neither_the_registry_nor_an_authority_is_not_governed(
    factory: sessionmaker[Session],
) -> None:
    """A standalone export or test plan has no registry to read and no authority to ask: its tools run as before."""
    entry = tool_entry("lookup_policy", "read")
    tool = Tool()

    result = Run(factory, entry, tool, lookup=False, context=False).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1


def test_an_exported_workflow_with_a_version_label_keeps_running_its_read_tools() -> None:
    """The exported-runtime entry point, given a ``workflow_version_id`` label but no registry: not refused."""
    from caliber.workflows.export_runtime import execute_exported_workflow

    entry = tool_entry("lookup_policy", "read")
    resolver = InMemoryToolResolver([entry])
    tool = Tool(result={"policy": "30-day refund"})
    resolver.register(entry, tool)
    ir = build_ir(parse_manifest(tool_manifest("lookup_policy")), resolver, version="1")

    result = execute_exported_workflow(
        ir,
        "refund policy",
        resolver=resolver,
        workflow_version_id="exported-version-1",
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1


def test_an_unreadable_registry_is_a_refusal_not_a_guess(factory: sessionmaker[Session]) -> None:
    entry = tool_entry("lookup_policy", "read")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool)

    def broken(_name: str, _version: str) -> None:
        raise ConnectionError(f"database said {SENTINEL}")

    run.plan.registered_tool_identity = broken

    result = run.execute()

    assert result.status == "error"
    assert "identity could not be read: ConnectionError" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert tool.calls == []


@pytest.mark.parametrize(
    "drift",
    [
        {"module_path": "another.module"},
        {"callable_name": "another_callable"},
    ],
)
def test_a_binding_that_does_not_describe_the_registry_row_is_refused(
    factory: sessionmaker[Session], drift: dict[str, str]
) -> None:
    entry = tool_entry("lookup_policy", "read")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool)
    binding = run.plan.ir.nodes["tool_node"].binding  # type: ignore[attr-defined]
    run.plan.ir.nodes["tool_node"].binding = replace(binding, **drift)  # type: ignore[attr-defined]

    result = run.execute()

    assert result.status == "error"
    assert "does not match its registry row" in (result.error or "")
    assert tool.calls == []


def test_a_row_that_is_another_backend_than_the_binding_is_refused(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("lookup_policy", "read")
    seed_row(factory, replace(entry, execution_backend="openapi_http"))
    tool = Tool()

    result = Run(factory, entry, tool).execute()

    assert result.status == "error"
    assert "does not match its registry row" in (result.error or "")
    assert tool.calls == []


def test_a_row_squatting_on_a_builtin_name_is_fail_closed_whatever_it_declares(
    factory: sessionmaker[Session],
) -> None:
    """A custom ``list_workdir_files`` that declares ``read`` is not the built-in and is not waved through."""
    entry = tool_entry("list_workdir_files", "read")
    seed_row(factory, entry, owner="@someone")
    tool = Tool()

    result = Run(factory, entry, tool, context=False).execute()

    assert result.status == "error"
    assert "requires a governed effect context" in (result.error or "")
    assert tool.calls == []


def test_a_squatter_that_declares_read_is_recorded_irreversible_write_not_the_builtins_read(
    factory: sessionmaker[Session],
) -> None:
    """With a governed context the squatter reaches ``submit_proposal``, which must not take the built-in's class."""
    entry = tool_entry("list_workdir_files", "read")
    seed_row(factory, entry, owner="@someone")
    tool = Tool()

    result = Run(factory, entry, tool).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    (proposal,) = proposals(factory)
    assert proposal.effect_class == "irreversible_write"


def test_a_squatter_named_like_a_refused_builtin_is_not_refused_by_name_and_is_fail_closed(
    factory: sessionmaker[Session],
) -> None:
    """The refusal is the built-in's, matched by trusted identity: an impostor is dispatched at the fail-closed class."""
    entry = tool_entry("grep_files", "read")
    seed_row(factory, entry, owner="@someone")
    tool = Tool()

    result = Run(factory, entry, tool).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    (proposal,) = proposals(factory)
    assert proposal.effect_class == "irreversible_write"


def test_a_builtin_row_edited_to_a_stricter_level_is_recorded_at_that_level(
    factory: sessionmaker[Session],
) -> None:
    """The record, not only the route, is at least as strict as the row's declared level."""
    with factory() as session:
        register_builtin_tools(session)
        row = session.execute(
            select(CaliberToolRegistry).where(CaliberToolRegistry.name == "list_workdir_files")
        ).scalar_one()
        row.side_effect_level = "external_action"
        session.commit()
    entry = tool_entry(
        "list_workdir_files",
        "read",
        module_path=_builtin_module("list_workdir_files"),
        callable_name="list_workdir_files",
    )
    tool = Tool()

    result = Run(factory, entry, tool).execute()

    assert result.status == "completed", result.error
    (proposal,) = proposals(factory)
    assert proposal.effect_class == "irreversible_write"


def test_the_real_builtin_read_tool_runs_inline(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        register_builtin_tools(session)
        session.commit()
    entry = tool_entry(
        "list_workdir_files",
        "read",
        module_path=_builtin_module("list_workdir_files"),
        callable_name="list_workdir_files",
    )
    tool = Tool(result={"files": []})

    result = Run(factory, entry, tool, context=False).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    assert proposals(factory) == []


def test_the_real_builtin_write_tool_is_dispatched_at_its_inventory_class(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        register_builtin_tools(session)
        session.commit()
    entry = tool_entry(
        "write_workdir_file",
        "write",
        module_path=_builtin_module("write_workdir_file"),
        callable_name="write_workdir_file",
    )
    tool = Tool()

    result = Run(factory, entry, tool).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    assert proposals(factory)[0].effect_class == "reversible_write"


@pytest.mark.parametrize("name", ["grep_files", "read_text_file", "sandbox_python"])
def test_a_builtin_the_reference_profile_refuses_never_runs_and_leaves_no_proposal(
    factory: sessionmaker[Session], name: str
) -> None:
    with factory() as session:
        register_builtin_tools(session)
        session.commit()
    entry = tool_entry(name, "read", module_path=_builtin_module(name), callable_name=name)
    tool = Tool()

    result = Run(factory, entry, tool).execute(f"path {SENTINEL}")

    assert result.status == "error"
    assert f"registered tool {name!r} refused: unsupported_effect_path" in (result.error or "")
    assert SENTINEL not in (result.error or "")
    assert tool.calls == []
    assert proposals(factory) == []


def _builtin_module(name: str) -> str:
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE_BY_NAME

    assert BUILTIN_TOOL_OWNER and BUILTIN_TOOL_VERSION == "1.0"
    return BUILTIN_TOOL_MODULE_BY_NAME[name]


# ---------------------------------------------------------------- what stays as it was


def test_a_plan_without_a_workflow_version_keeps_calling_tools_directly(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()

    result = Run(
        factory, entry, tool, workflow_version_id=None, lookup=False, context=False
    ).execute()

    assert result.status == "completed", result.error
    assert len(tool.calls) == 1
    assert proposals(factory) == []


def test_a_preview_never_dispatches_and_does_not_run_a_write_tool(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool)

    result = run.execute(preview=True)

    assert tool.calls == [], "preview stubs a write tool exactly as before"
    assert run.authorized == []
    assert proposals(factory) == []
    assert result.status in {"completed", "error"}


@pytest.mark.parametrize(
    "drift", [{"execution_backend": "openapi_http"}, {"binding_type": "mcp_tool"}]
)
def test_other_registry_backends_are_left_to_their_own_tasks(drift: dict[str, str]) -> None:
    """OpenAPI and MCP tools are routed by their own B04 tasks; this path must not claim them."""
    binding = replace(_plain_binding(), **drift)
    plan = RuntimePlan(
        ir=None,  # type: ignore[arg-type]
        resolver=InMemoryToolResolver([]),
        workflow_version_id="v",
    )
    assert runtime_module._governed_tool_route(plan, binding, node_id="n", preview=False) is None


def _plain_binding() -> Any:
    from caliber.workflows.ir import IRToolBinding

    return IRToolBinding(
        local_name="x",
        registry_ref="tool.x.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level="write",
        allow_in_preview=False,
        module_path="m",
        callable_name="x",
    )


# ---------------------------------------------------------------- backend unit checks


def _recorded_request(
    *, view: dict[str, Any] | None = None, args: Any = None, **intent_args: Any
) -> Any:
    """A backend request: the proposal's view and its recorded intent (``args`` replaces the whole args value)."""
    from types import SimpleNamespace

    base_view = {
        "connector": "registered_tool",
        "connector_version": "1",
        "resource": "registered-tool/open_ticket/1.0",
        "logical_key": "key",
        "attempt": 1,
        "intent_hash": "sha256:" + "1" * 64,
    }
    recorded: Any = {
        "tool": "open_ticket",
        "version": "1.0",
        "arguments": {"a": 1},
        "input": "text",
        **intent_args,
    }
    return SimpleNamespace(
        view=SimpleNamespace(**{**base_view, **(view or {})}),
        intent={"args": recorded if args is None else args},
    )


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        "invoker": lambda arguments, input_text: (arguments, input_text),
        "receipt_verifier": verifier,
        **overrides,
    }


@pytest.mark.parametrize(
    ("request_overrides", "config_overrides", "code"),
    [
        ({"view": {"connector": "http"}}, {}, "connector_binding_mismatch"),
        ({"view": {"connector_version": "2"}}, {}, "connector_binding_mismatch"),
        ({}, {"invoker": None}, "backend_config_missing"),
        ({}, {"receipt_verifier": "no"}, "backend_config_missing"),
        ({"args": "no"}, {}, "connector_intent_unsupported"),
        ({"tool": 5}, {}, "connector_intent_unsupported"),
        ({"version": None}, {}, "connector_intent_unsupported"),
        ({"arguments": []}, {}, "connector_intent_unsupported"),
        ({"input": 5}, {}, "connector_intent_unsupported"),
        ({"view": {"resource": "registered-tool/other/1.0"}}, {}, "connector_intent_unsupported"),
    ],
)
def test_the_backend_refuses_before_calling_anything_it_cannot_vouch_for(
    request_overrides: dict[str, Any], config_overrides: dict[str, Any], code: str
) -> None:
    called: list[str] = []
    config = _config(invoker=lambda *_a: called.append("ran"))
    config.update(config_overrides)
    request = _recorded_request(**request_overrides)

    with pytest.raises(dispatcher_module._TransportRefusedError) as raised:
        BACKENDS["registered_tool"].backend.perform(request, config)  # type: ignore[union-attr]

    assert raised.value.code == code
    assert called == []


def test_the_backend_with_no_config_is_refused() -> None:
    with pytest.raises(dispatcher_module._TransportRefusedError) as raised:
        BACKENDS["registered_tool"].backend.perform(_recorded_request(), None)  # type: ignore[union-attr]
    assert raised.value.code == "backend_config_missing"


def test_a_verifier_returning_something_else_than_receipt_facts_is_refused() -> None:
    config = _config(receipt_verifier=lambda _request, _response: {"not": "facts"})
    result = BACKENDS["registered_tool"].backend.perform(_recorded_request(), config)  # type: ignore[union-attr]
    assert result.response == {"result": ({"a": 1}, "text")}
    with pytest.raises(dispatcher_module._TransportRefusedError) as raised:
        assert result.verify is not None
        result.verify()
    assert raised.value.code == "receipt_verifier_invalid"


def test_the_backend_calls_the_invoker_with_the_recorded_arguments() -> None:
    result = BACKENDS["registered_tool"].backend.perform(_recorded_request(), _config())  # type: ignore[union-attr]
    assert result.response == {"result": ({"a": 1}, "text")}
    assert result.outcome.outcome.value == "committed_receipt"


def test_dispatching_a_registered_tool_proposal_without_an_approval_id_is_refused(
    factory: sessionmaker[Session],
) -> None:
    """The dispatcher itself refuses a registered-tool proposal that has no approval; the tool is never called."""
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool, authorize=lambda _proposal: None)
    run.execute()
    (proposal_id,) = run.authorized
    with factory() as session:
        lease = leased(session, "runtime-owner")
        session.commit()

    with pytest.raises(EffectRefused) as raised:
        dispatch(
            factory,
            proposal_id,
            approval_id=None,
            lease=lease,
            backend_kind="registered_tool",
            backend_config={"invoker": tool, "receipt_verifier": verifier},
            approval_validator=runtime_module.approval_valid_at_commit,
        )

    assert raised.value.code == "proposal_not_approved"
    assert tool.calls == []


# ---------------------------------------------------------------- races (PostgreSQL row locks)


def test_two_executors_of_one_node_run_the_tool_once(
    each_dialect: sessionmaker[Session], request: pytest.FixtureRequest
) -> None:
    """Both executors propose the same logical effect; the claim lock lets exactly one dispatch."""
    if request.node.callspec.id == "sqlite":
        pytest.skip("the claim lock is a PostgreSQL row lock; SQLite serializes writers anyway")
    factory = each_dialect
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    barrier = threading.Barrier(2, timeout=30)
    shared: dict[str, EffectAuthorization] = {}
    guard = threading.Lock()
    outcomes: list[Any] = []

    def authorize(proposal: Any) -> EffectAuthorization:
        with guard:  # one exact-intent approval, presented by both executors
            if "approval" not in shared:
                shared["approval"] = grant(factory, proposal.proposal_id)
        barrier.wait()  # both executors hold the proposal and the approval before either dispatches
        return shared["approval"]

    def one() -> None:
        outcomes.append(Run(factory, entry, tool, authorize=authorize).execute())

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == 2
    assert sorted(result.status for result in outcomes) == ["completed", "error"]
    assert len(tool.calls) == 1
    (proposal,) = proposals(factory)
    assert proposal.state == "committed"
