"""B04.6 (#768): in a governed agent step a model's protected tool call is a **proposal**, bound to the tool's trusted identity.

The model proposes one action. The platform finds the agent's tool from the connector and resource the model named, takes the
connector, resource and intent arguments from the tool's registry row (or the MCP server record and the administrator's tool
policy), validates the model's arguments against the trusted input schema before anything is recorded, refuses a claim that
differs, records one ``proposed`` effect with its approval request and parks the run. Nothing is sent: the downstream counters
stay at zero. The recorded intent is the one a workflow tool node records for the same call, so the connector's backend can
dispatch it once it is approved (shown here for a registered tool).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

import caliber.workflows.runtime as runtime_module
from caliber.contracts.agent_step import ProposedAction
from caliber.db.models import CaliberToolRegistry
from caliber.effects.connectors import mcp_tool, openapi_http
from caliber.effects.dispatcher import EffectRefused, UnsupportedEffectPath, dispatch
from caliber.effects.model_tool_calls import ToolProposal
from caliber.effects.registered_tools import REGISTERED_TOOL_CONNECTOR, tool_resource
from caliber.effects.step_proposal import (
    GovernedStepContext,
    ToolTrust,
    record_step_proposal,
    registry_tool_classifier,
)
from caliber.mcp_policy import TOOL_UNCLASSIFIED
from caliber.workflows.agent_step import ScriptedStep, answer
from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE, register_builtin_tools
from caliber.workflows.runtime import FakeWorkflowExecutor, execute
from caliber.workflows.tools import ToolRegistryEntry
from tests.mcp_fakes import SERVER_ID, FakeMcp, seed_server
from tests.test_governed_agent_step import Recorder
from tests.test_model_selected_calls import (
    LIMITS,
    LOOPBACK,
    SENTINEL,
    USAGE,
    World,
    agent_manifest,
    factory,  # noqa: F401 - fixtures
    mcp,  # noqa: F401
    mcp_binding,
    registered,
)
from tests.test_openapi_governed_dispatch import Server, config, entry_for
from tests.test_openapi_governed_dispatch import seed_row as seed_openapi_row
from tests.test_registered_tool_dispatch import RUN_ID, Tool, grant, seed_row, tool_entry, verifier

pytestmark = pytest.mark.usefixtures("fixed_step_clock")

FAIL_CLOSED = "irreversible_write"
TICKET = {"title": "disk full"}
TICKET_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "maxLength": 40}},
    "required": ["title"],
    "additionalProperties": False,
}


def context_for(factory: sessionmaker[Session], **changes: Any) -> GovernedStepContext:  # noqa: F811
    fields: dict[str, Any] = {
        "session_factory": factory,
        "workflow_run_id": RUN_ID,
        "run_manifest_digest": "a" * 64,
        "grant_id": "grant-1",
        "classify_tool": registry_tool_classifier(factory),
        "context_manifest_digest": lambda _step: "b" * 64,
        "limits": lambda _agent: LIMITS,
        "requested_by": "editor-1",
        "approval_ttl_seconds": 3600,
    }
    fields.update(changes)
    return GovernedStepContext(**fields)


def governed(
    factory: sessionmaker[Session],  # noqa: F811
    manifest: dict[str, Any],
    entries: list[ToolRegistryEntry],
    **context: Any,
) -> World:
    return World(
        factory,
        manifest,
        entries,
        context_recorder=Recorder(),
        governed_agent_steps=context_for(factory, **context),
    )


def propose(world: World, action: ProposedAction, **tools: Any) -> Any:
    script = [ScriptedStep("proposed_action", (action,), usage=USAGE)]
    return execute(
        world.plan,
        "go",
        executor=FakeWorkflowExecutor(step_script=script),
        extra_tools=tools or None,
    )


def parked(world: World, result: Any) -> tuple[Any, Any]:
    assert result.status == "blocked" and result.error == "waiting_approval:agent", result.error
    proposals, requests = world.rows()
    assert len(proposals) == 1 and len(requests) == 1
    assert proposals[0].state == "proposed" and requests[0].status == "pending"
    assert requests[0].proposal_id == proposals[0].proposal_id
    assert requests[0].intent_hash == proposals[0].intent_hash
    return proposals[0], requests[0]


def refused_with(world: World, result: Any, code: str) -> None:
    assert result.status == "error"
    assert result.error == f"ToolExecutionError: governed agent step proposal refused: {code}", (
        result.error
    )
    assert world.rows() == ([], []), "a refusal records no proposal and asks for no approval"


# -------------------------------------------------------------------------------------- a registered tool


def _registered_action(**changes: Any) -> ProposedAction:
    fields: dict[str, Any] = {
        "connector": REGISTERED_TOOL_CONNECTOR,
        "connector_version": "1",
        "effect_class": FAIL_CLOSED,
        "resource": tool_resource("open_ticket", "1.0"),
        "args": TICKET,
    }
    fields.update(changes)
    return ProposedAction(**fields)


def _registered(
    factory: sessionmaker[Session],  # noqa: F811
    *,
    level: str = "write",
    schema: dict[str, Any] | None = TICKET_SCHEMA,
    mapped: bool = True,
    extra: dict[str, str] | None = None,
) -> World:
    entry = replace(tool_entry("open_ticket", level), input_schema=schema)
    seed_row(factory, entry)
    with factory() as session:
        row = session.query(CaliberToolRegistry).filter_by(name="open_ticket").one()
        row.input_schema = schema
        session.commit()
    backends = {("open_ticket", "1.0"): "registered_tool"} if mapped else {}
    return governed(
        factory,
        agent_manifest({"open_ticket": registered("open_ticket")}),
        [entry],
        tool_backends={**backends, **(extra or {})},
    )


def test_a_registered_write_tool_the_model_proposes_is_recorded_and_the_run_parks(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    tool = Tool()
    result = propose(world, _registered_action(), open_ticket=tool)

    proposal, _ = parked(world, result)
    assert tool.calls == [], "the model never ran the tool"
    assert (proposal.connector, proposal.connector_version) == (REGISTERED_TOOL_CONNECTOR, "1")
    assert proposal.resource == tool_resource("open_ticket", "1.0")
    assert proposal.effect_class == FAIL_CLOSED
    # the node's own intent shape (``tool_intent_args``): the registered_tool backend can dispatch it as is
    assert proposal.args == {
        "tool": "open_ticket",
        "version": "1.0",
        "arguments": TICKET,
        "input": "",
    }
    (step,) = [s for s in result.steps if s.node_id == "agent"]
    assert "disk full" not in repr(step.checkpoint_state), (
        "the checkpoint names the proposal, not its arguments"
    )


def test_the_recorded_proposal_dispatches_through_the_registered_tool_backend_once_approved(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    parked(world, propose(world, _registered_action()))
    (proposal,) = world.rows()[0]
    tool = Tool()
    authorization = grant(factory, proposal.proposal_id)
    outcome = dispatch(
        factory,
        proposal.proposal_id,
        approval_id=authorization.approval_id,
        approver_scopes=authorization.approver_scopes,
        lease=authorization.lease,
        backend_kind="registered_tool",
        backend_config={
            "invoker": lambda arguments, _input: tool(**arguments),
            "receipt_verifier": verifier,
        },
        evidence_hash=authorization.evidence_hash,
        approval_validator=runtime_module.approval_valid_at_commit,
    )
    assert outcome.state == "committed"
    assert tool.calls == [((), TICKET)], "what ran is what the approver was shown"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"effect_class": "read"}, "claim_mismatch"),
        ({"effect_class": "reversible_write"}, "claim_mismatch"),
        ({"connector_version": "9"}, "claim_mismatch"),
        ({"expected_resource_version": "v7"}, "claim_mismatch"),
        ({"resource": tool_resource("escalate", "1.0")}, "resource_not_admitted"),
        ({"args": {"title": 5}}, "tool_input_invalid"),
        ({"args": {"title": "x" * 41}}, "tool_input_invalid"),
        ({"args": {"title": "ok", "extra": SENTINEL}}, "tool_input_invalid"),
        ({"args": {}}, "tool_input_invalid"),
    ],
)
def test_a_registered_proposal_is_refused_when_the_models_claim_or_arguments_are_not_the_trusted_ones(
    factory: Any,  # noqa: F811
    changes: dict[str, Any],
    code: str,
) -> None:
    world = _registered(factory)
    result = propose(world, _registered_action(**changes))
    refused_with(world, result, code)
    assert SENTINEL not in (result.error or "")


def test_a_tool_the_producer_did_not_map_is_never_proposable(factory: Any) -> None:  # noqa: F811
    world = _registered(factory, mapped=False)
    refused_with(world, propose(world, _registered_action()), "connector_not_mapped")


def test_a_tool_mapped_to_another_backend_is_not_proposable_through_this_one(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory, mapped=False, extra={"open_ticket": "x"})
    world.plan.governed_agent_steps = context_for(
        factory, tool_backends={("open_ticket", "1.0"): "document_request"}
    )
    refused_with(world, propose(world, _registered_action()), "connector_not_mapped")


def test_the_mapping_is_keyed_by_registry_identity_not_by_the_local_name(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory, mapped=False)
    world.plan.governed_agent_steps = context_for(
        factory, tool_backends={("open_ticket", "9.9"): "registered_tool"}
    )
    refused_with(world, propose(world, _registered_action()), "connector_not_mapped")


def test_a_read_class_tool_is_run_not_proposed(factory: Any) -> None:  # noqa: F811
    world = _registered(factory, level="read")
    refused_with(world, propose(world, _registered_action()), "tool_not_protected")


def test_a_built_in_the_inventory_refuses_cannot_be_proposed(factory: Any) -> None:  # noqa: F811
    with factory() as session:
        register_builtin_tools(session)
        session.commit()
    entry = ToolRegistryEntry(
        name="read_text_file",
        version="1.0",
        module_path=BUILTIN_TOOL_MODULE,
        callable_name="read_text_file",
        side_effect_level="read",
    )
    world = governed(
        factory,
        agent_manifest({"read_text_file": registered("read_text_file")}),
        [entry],
        tool_backends={("read_text_file", "1.0"): "registered_tool"},
    )
    action = _registered_action(resource=tool_resource("read_text_file", "1.0"), args={})
    result = propose(world, action)
    assert result.status == "error"
    assert "unsupported_effect_path" in (result.error or "")
    assert world.rows() == ([], [])


@pytest.mark.parametrize("connector", ["mcp-tool", "openapi-http", "http"])
def test_a_proposal_that_names_a_connector_no_tool_of_the_agent_sends_through_is_refused(
    factory: Any,  # noqa: F811
    connector: str,
) -> None:
    world = _registered(factory)
    refused_with(
        world, propose(world, _registered_action(connector=connector)), "connector_not_mapped"
    )


# ------------------------------------------------------------------------------------------- an OpenAPI tool


def _openapi(
    factory: Any,  # noqa: F811
    url: str,
    *,
    schema: dict[str, Any] | None = None,
    egress: Any = "default",
    **cfg: Any,
) -> World:
    entry = entry_for(
        "file_ticket",
        "write",
        config(url, **cfg),
        schema
        or {
            "type": "object",
            "properties": {"body": {"type": "object", "required": ["summary"]}},
            "required": ["body"],
            "additionalProperties": False,
        },
    )
    seed_openapi_row(factory, entry)
    extra = {} if egress == "default" else {"egress_policy": egress}
    return World(
        factory,
        agent_manifest({"file_ticket": registered("file_ticket")}),
        [entry],
        context_recorder=Recorder(),
        governed_agent_steps=context_for(
            factory, tool_backends={("file_ticket", "1.0"): "openapi_http"}
        ),
        **extra,
    )


def _openapi_action(url: str, **changes: Any) -> ProposedAction:
    fields: dict[str, Any] = {
        "connector": openapi_http.CONNECTOR,
        "connector_version": "1",
        "effect_class": FAIL_CLOSED,
        "resource": f"openapi/POST {url}/tickets",
        "args": {"body": {"summary": "disk full"}},
    }
    fields.update(changes)
    return ProposedAction(**fields)


def test_an_openapi_write_the_model_proposes_is_recorded_and_nothing_is_sent(
    factory: Any,  # noqa: F811
) -> None:
    with Server() as server:
        world = _openapi(factory, server.url)
        proposal, _ = parked(world, propose(world, _openapi_action(server.url)))
        assert server.requests == [], "no request left the platform"
    assert (proposal.connector, proposal.connector_version) == (openapi_http.CONNECTOR, "1")
    assert proposal.resource == f"openapi/POST {server.url}/tickets"
    assert proposal.effect_class == FAIL_CLOSED
    assert proposal.args == {
        "tool": "file_ticket",
        "version": "1.0",
        "input": {"body": {"summary": "disk full"}},
    }


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"args": {"body": {"nope": 1}}}, "openapi_input_invalid"),
        ({"args": {"body": {"summary": "x"}, "extra": SENTINEL}}, "openapi_input_invalid"),
        ({"effect_class": "reversible_write"}, "claim_mismatch"),
        ({"resource": "openapi/DELETE http://169.254.169.254/tickets"}, "resource_not_admitted"),
    ],
)
def test_an_openapi_proposal_is_refused_before_it_is_recorded(
    factory: Any,  # noqa: F811
    changes: dict[str, Any],
    code: str,
) -> None:
    with Server() as server:
        world = _openapi(factory, server.url)
        result = propose(world, _openapi_action(server.url, **changes))
        refused_with(world, result, code)
        assert server.requests == []


def test_a_caller_supplied_idempotency_key_is_refused_not_recorded(factory: Any) -> None:  # noqa: F811
    with Server() as server:
        world = _openapi(factory, server.url, schema={"type": "object"})
        action = _openapi_action(
            server.url, args={"body": {}, "header_params": {"Idempotency-Key": "mine"}}
        )
        refused_with(world, propose(world, action), "openapi_key_override_refused")


def test_an_openapi_destination_the_egress_policy_refuses_is_refused_at_proposal_time(
    factory: Any,  # noqa: F811
) -> None:
    from caliber.egress import EgressPolicy

    with Server() as server:
        world = _openapi(
            factory, server.url, egress=EgressPolicy(allowed_hosts=frozenset({"example.com"}))
        )
        refused_with(world, propose(world, _openapi_action(server.url)), "openapi_egress_blocked")


# ------------------------------------------------------------------------------------------------ an MCP tool


def _mcp(factory: Any, **context: Any) -> World:  # noqa: F811
    seed_server(factory, tools={"mystery": None})
    manifest = agent_manifest(
        {
            "lookup_docs": mcp_binding("lookup"),
            "write_issue": mcp_binding("issue_write"),
            "mystery": mcp_binding("mystery"),
        }
    )
    allowed = {
        "mcp_tools": frozenset(
            {(SERVER_ID, "issue_write"), (SERVER_ID, "lookup"), (SERVER_ID, "mystery")}
        )
    }
    return governed(factory, manifest, [], **{**allowed, **context})


def _mcp_action(tool: str = "issue_write", **changes: Any) -> ProposedAction:
    fields: dict[str, Any] = {
        "connector": mcp_tool.CONNECTOR,
        "connector_version": "1",
        "effect_class": FAIL_CLOSED,
        "resource": mcp_tool.mcp_resource(SERVER_ID, tool),
        "args": {"title": "disk full"},
    }
    fields.update(changes)
    return ProposedAction(**fields)


def test_an_mcp_write_the_model_proposes_is_recorded_and_the_server_is_never_reached(
    factory: Any,  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    world = _mcp(factory)
    proposal, _ = parked(world, propose(world, _mcp_action()))
    assert mcp.calls == []
    assert (proposal.connector, proposal.resource) == (
        mcp_tool.CONNECTOR,
        mcp_tool.mcp_resource(SERVER_ID, "issue_write"),
    )
    assert proposal.effect_class == FAIL_CLOSED
    args = dict(proposal.args)
    assert args.pop("server_digest").startswith("sha256:")
    assert args == {
        "server_id": SERVER_ID,
        "tool": "issue_write",
        "arguments": {"title": "disk full"},
    }


def test_an_mcp_tool_outside_the_producers_allow_list_is_not_proposable(
    factory: Any,  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    world = _mcp(factory, mcp_tools=frozenset())
    refused_with(world, propose(world, _mcp_action()), "connector_not_mapped")
    assert mcp.calls == []


@pytest.mark.parametrize(
    ("tool", "code"),
    [("lookup", "tool_not_protected"), ("mystery", TOOL_UNCLASSIFIED)],
)
def test_an_mcp_read_is_run_and_an_unclassified_tool_is_refused_not_proposed(
    factory: Any,  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
    tool: str,
    code: str,
) -> None:
    world = _mcp(factory)
    refused_with(world, propose(world, _mcp_action(tool)), code)
    assert mcp.calls == []


def test_the_servers_own_claims_do_not_make_a_write_tool_a_read(
    factory: Any,  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    """``seed_server`` makes the server report ``readOnlyHint`` for every tool; the administrator's policy says ``write``."""
    world = _mcp(factory)
    parked(world, propose(world, _mcp_action("issue_write")))


# ----------------------------------------------------------------------------- the service, with a fake binder


class _Binder:
    def __init__(self, proposal: ToolProposal, connector: str = "registered_tool") -> None:
        self.proposal = proposal
        self._connector = connector

    def connector(self, _binding: Any) -> str | None:
        return self._connector

    def static_resource(self, _binding: Any) -> str | None:
        return None

    def bind(self, _binding: Any, _arguments: Any) -> ToolProposal:
        return self.proposal


def test_a_registry_row_that_changed_between_classification_and_proposal_is_refused(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    agent = world.agent()
    proposal = ToolProposal(
        backend_kind="registered_tool",
        connector=REGISTERED_TOOL_CONNECTOR,
        resource=tool_resource("open_ticket", "1.0"),
        args={},
        tool_name="open_ticket",
        caliber_defined=False,
        effect_floor="reversible_write",
        registry=("open_ticket", "2.0"),
    )
    context = world.plan.governed_agent_steps
    assert context is not None
    request = replace(_request(), node_id="agent")
    trust = {
        "open_ticket": ToolTrust(
            "open_ticket", FAIL_CLOSED, False, "dispatch", ("open_ticket", "1.0")
        )
    }
    with pytest.raises(EffectRefused) as stopped:
        record_step_proposal(
            context,
            request,
            answer(
                request, "proposed_action", usage=USAGE, proposed_actions=(_registered_action(),)
            ),
            trust,
            binder=_Binder(proposal),
            bindings=agent.tools,
        )
    assert stopped.value.code == "registry_row_mismatch"


def _request() -> Any:
    from caliber.workflows.agent_step import AgentStepRequest

    return AgentStepRequest(
        step_request_id="step-1",
        workflow_run_id=RUN_ID,
        node_id="agent",
        occurrence_id="occ-1",
        attempt=1,
        run_manifest_digest="a" * 64,
        context_manifest_digest="b" * 64,
        grant_id="grant-1",
        limits=LIMITS,
    )


def test_two_tools_that_derive_different_proposals_for_one_resource_are_ambiguous(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    context = world.plan.governed_agent_steps
    assert context is not None
    base = ToolProposal(
        backend_kind="registered_tool",
        connector=REGISTERED_TOOL_CONNECTOR,
        resource=tool_resource("open_ticket", "1.0"),
        args={"a": 1},
        tool_name="open_ticket",
        caliber_defined=False,
        effect_floor="reversible_write",
        registry=("open_ticket", "1.0"),
    )

    class Twin(_Binder):
        def __init__(self) -> None:
            super().__init__(base)
            self.calls = 0

        def bind(self, _binding: Any, _arguments: Any) -> ToolProposal:
            self.calls += 1
            return replace(base, args={"a": self.calls})

    agent = world.agent()
    twin_bindings = [agent.tools[0], replace(agent.tools[0], local_name="open_ticket_alias")]
    trust = {
        binding.local_name: ToolTrust(
            binding.local_name, FAIL_CLOSED, False, "dispatch", ("open_ticket", "1.0")
        )
        for binding in twin_bindings
    }
    request = _request()
    with pytest.raises(EffectRefused) as stopped:
        record_step_proposal(
            context,
            request,
            answer(
                request, "proposed_action", usage=USAGE, proposed_actions=(_registered_action(),)
            ),
            trust,
            binder=Twin(),
            bindings=twin_bindings,
        )
    assert stopped.value.code == "connector_ambiguous"


def test_a_builtin_whose_row_declares_a_stricter_level_is_classified_like_a_node_would(
    factory: Any,  # noqa: F811
) -> None:
    """The step classifier takes the stricter of the inventory's class and the row's declared level, as a tool node does."""
    from caliber.effects.registered_tools import (
        load_registered_tool_identity,
        route_registered_tool,
    )
    from tests.test_governed_agent_step import _binding

    with factory() as session:
        register_builtin_tools(session)
        session.commit()
    classify = registry_tool_classifier(factory)
    assert classify(_binding()).effect_class == "read", (
        "the control: the seeded row agrees with the inventory"
    )
    with factory() as session:
        row = session.query(CaliberToolRegistry).filter_by(name="list_workdir_files").one()
        row.side_effect_level = "write"
        session.commit()
    trust = classify(_binding())
    with factory() as session:
        identity = load_registered_tool_identity(session, name="list_workdir_files", version="1.0")
    assert trust.effect_class == "reversible_write"
    assert trust.effect_class == route_registered_tool(identity).effect_class


def test_a_trusted_schema_that_is_itself_invalid_accepts_nothing(factory: Any) -> None:  # noqa: F811
    world = _registered(factory, schema={"type": 5})
    refused_with(world, propose(world, _registered_action()), "tool_input_invalid")


def test_a_row_with_no_input_schema_accepts_any_object_but_nothing_else(factory: Any) -> None:  # noqa: F811
    world = _registered(factory, schema=None)
    parked(world, propose(world, _registered_action(args={"anything": ["goes", 1]})))


def test_the_binder_names_no_connector_for_a_binding_it_cannot_classify(factory: Any) -> None:  # noqa: F811
    world = _registered(factory)
    (binding,) = world.agent().tools
    binder = runtime_module._RuntimeToolBinder(world.plan, "agent")
    other_backend = replace(binding, execution_backend="something_else")
    assert binder.connector(other_backend) is None
    assert binder.static_resource(other_backend) is None


def test_the_binder_refuses_what_it_cannot_classify_with_a_fixed_code(factory: Any) -> None:  # noqa: F811
    """Each way the trusted identity can be missing or not the binding's is a code, never a proposal."""
    world = _registered(factory)
    (binding,) = world.agent().tools
    binder = runtime_module._RuntimeToolBinder(world.plan, "agent")

    other_backend = replace(binding, execution_backend="something_else")
    with pytest.raises(UnsupportedEffectPath):
        binder.bind(other_backend, TICKET)

    wrong_module = replace(binding, module_path="evil.module")
    with pytest.raises(EffectRefused) as mismatch:
        binder.bind(wrong_module, TICKET)
    assert mismatch.value.code == "registry_row_mismatch"

    unresolved = replace(binding, resolved_name="", resolved_version="")
    with pytest.raises(EffectRefused) as unreadable:
        binder.bind(unresolved, TICKET)
    assert unreadable.value.code == "registry_row_unreadable"


def test_an_openapi_tool_the_row_calls_read_is_run_not_proposed_and_one_without_a_row_is_refused(
    factory: Any,  # noqa: F811
) -> None:
    with Server() as server:
        read = entry_for("list_tickets", "read", config(server.url, method="GET"))
        seed_openapi_row(factory, read)
        world = governed(
            factory,
            agent_manifest({"list_tickets": registered("list_tickets")}),
            [read],
            tool_backends={("list_tickets", "1.0"): "openapi_http"},
        )
        world.plan.egress_policy = LOOPBACK
        binder = runtime_module._RuntimeToolBinder(world.plan, "agent")
        (binding,) = world.agent().tools
        with pytest.raises(EffectRefused) as inline:
            binder.bind(binding, {})
        assert inline.value.code == "tool_not_protected"

        unresolved = replace(binding, resolved_name="", resolved_version="")
        with pytest.raises(UnsupportedEffectPath):
            binder.bind(unresolved, {})


def test_the_runtime_binder_describes_only_the_kinds_it_can_classify(factory: Any) -> None:  # noqa: F811
    world = _registered(factory)
    binder = runtime_module._RuntimeToolBinder(world.plan, "agent")
    (binding,) = world.agent().tools
    assert binder.connector(binding) == REGISTERED_TOOL_CONNECTOR
    assert binder.static_resource(binding) == tool_resource("open_ticket", "1.0")
    odd = replace(binding, binding_type="something_else")
    assert binder.connector(odd) is None and binder.static_resource(odd) is None
    with pytest.raises(UnsupportedEffectPath):
        binder.bind(odd, {})
    assert json.dumps(
        binder.bind(binding, TICKET).args
    )  # a protected registered tool binds, args are plain JSON
