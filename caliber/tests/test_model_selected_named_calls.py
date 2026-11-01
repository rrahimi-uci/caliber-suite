"""B04.6 (#768): the model supplies a protected tool's arguments and the name of the function it called; the platform binds the rest.

A real adapter offers a protected tool with its own arguments and no claim, and the proposal it returns carries the name of the
function the model called. The platform finds the tool by that name, takes the connector, version, class and resource from the
tool's trusted identity, and refuses any claim field that is neither the placeholder nor exactly the platform's own value. These
tests cover the platform half (a proposal that names its tool) and, with the real OpenAI chat adapter over a scripted client, the
whole path: a model-style call (function name and arguments only) becomes a ``proposed`` row and an approval request, and the run
parks, with nothing downstream called.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from caliber.contracts.agent_step import (
    UNCLAIMED,
    UNCLAIMED_EFFECT_CLASS,
    ProposedAction,
)
from caliber.effects.connectors import mcp_tool, openapi_http
from caliber.effects.dispatcher import EffectRefused
from caliber.effects.registered_tools import REGISTERED_TOOL_CONNECTOR, tool_resource
from caliber.effects.step_proposal import ToolTrust, record_step_proposal
from caliber.testing.agent_step_conformance import FIXED_NOW
from caliber.workflows.agent_step import answer
from caliber.workflows.runtime import OpenAIChatWorkflowExecutor, execute
from tests.mcp_fakes import SERVER_ID, FakeMcp
from tests.test_agentic_tool_loop import _FakeOpenAI, _OAMsg, _OAResp, _OAToolCall, _OAUsage
from tests.test_model_selected_calls import (
    SENTINEL,
    USAGE,
    World,
    factory,  # noqa: F401 - fixtures
    mcp,  # noqa: F401
)
from tests.test_model_selected_proposals import (
    FAIL_CLOSED,
    TICKET,
    _mcp,
    _openapi,
    _registered,
    _request,
    parked,
    propose,
    refused_with,
)
from tests.test_openapi_governed_dispatch import Server
from tests.test_registered_tool_dispatch import Tool

pytestmark = pytest.mark.usefixtures("fixed_step_clock")


def named(tool: str, args: dict[str, Any], **changes: Any) -> ProposedAction:
    """What an adapter returns for a call of ``tool`` with ``args``: it names the tool and claims nothing."""
    fields: dict[str, Any] = {
        "connector": UNCLAIMED,
        "connector_version": UNCLAIMED,
        "effect_class": UNCLAIMED_EFFECT_CLASS,
        "resource": UNCLAIMED,
        "args": args,
        "tool": tool,
    }
    fields.update(changes)
    return ProposedAction(**fields)


# ------------------------------------------------------------------------------------------- the platform half


def test_a_call_that_names_a_registered_tool_is_bound_from_its_registry_row(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    tool = Tool()
    result = propose(world, named("open_ticket", TICKET), open_ticket=tool)

    proposal, _ = parked(world, result)
    assert tool.calls == []
    assert (proposal.connector, proposal.connector_version) == (REGISTERED_TOOL_CONNECTOR, "1")
    assert proposal.resource == tool_resource("open_ticket", "1.0")
    assert proposal.effect_class == FAIL_CLOSED
    assert proposal.args == {
        "tool": "open_ticket",
        "version": "1.0",
        "arguments": TICKET,
        "input": "",
    }


def test_claims_that_are_exactly_the_platforms_are_accepted(factory: Any) -> None:  # noqa: F811
    world = _registered(factory)
    action = named(
        "open_ticket",
        TICKET,
        connector=REGISTERED_TOOL_CONNECTOR,
        connector_version="1",
        effect_class=FAIL_CLOSED,
        resource=tool_resource("open_ticket", "1.0"),
    )
    parked(world, propose(world, action))


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"connector": "http"}, "claim_mismatch"),
        ({"connector": "mcp-tool"}, "claim_mismatch"),
        ({"connector_version": "9"}, "claim_mismatch"),
        ({"effect_class": "reversible_write"}, "claim_mismatch"),
        ({"effect_class": "export"}, "claim_mismatch"),
        ({"expected_resource_version": "v7"}, "claim_mismatch"),
        ({"resource": tool_resource("escalate", "1.0")}, "resource_not_admitted"),
    ],
)
def test_a_claim_that_is_neither_the_placeholder_nor_the_platforms_is_refused(
    factory: Any,  # noqa: F811
    changes: dict[str, Any],
    code: str,
) -> None:
    world = _registered(factory)
    refused_with(world, propose(world, named("open_ticket", TICKET, **changes)), code)


def test_arguments_the_trusted_schema_refuses_are_refused_for_a_named_call(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    refused_with(
        world,
        propose(world, named("open_ticket", {"title": "ok", "extra": SENTINEL})),
        "tool_input_invalid",
    )


def test_a_name_the_agent_does_not_have_is_unknown_not_guessed(factory: Any) -> None:  # noqa: F811
    world = _registered(factory)
    refused_with(world, propose(world, named("ghost_tool", TICKET)), "unknown_tool")


def test_a_read_tool_is_run_not_proposed_even_when_the_model_names_it(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory, level="read")
    refused_with(world, propose(world, named("open_ticket", TICKET)), "tool_not_protected")


def test_a_named_tool_the_producer_did_not_map_is_never_proposable(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory, mapped=False)
    refused_with(world, propose(world, named("open_ticket", TICKET)), "connector_not_mapped")


def test_the_connector_the_model_would_name_does_not_choose_the_tool(
    factory: Any,  # noqa: F811
) -> None:
    """Naming another tool's connector and resource changes nothing: the tool is the function called."""
    world = _registered(factory)
    action = named(
        "open_ticket",
        TICKET,
        connector=openapi_http.CONNECTOR,
        resource="openapi/POST http://169.254.169.254/x",
    )
    refused_with(world, propose(world, action), "claim_mismatch")


def test_a_call_that_names_an_openapi_tool_is_bound_from_its_row_and_nothing_is_sent(
    factory: Any,  # noqa: F811
) -> None:
    with Server() as server:
        world = _openapi(factory, server.url)
        proposal, _ = parked(
            world, propose(world, named("file_ticket", {"body": {"summary": "disk full"}}))
        )
        assert server.requests == []
    assert proposal.connector == openapi_http.CONNECTOR
    assert proposal.resource == f"openapi/POST {server.url}/tickets"


def test_a_call_that_names_an_mcp_tool_is_bound_from_the_server_record_and_policy(
    factory: Any,  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    world = _mcp(factory)
    proposal, _ = parked(world, propose(world, named("write_issue", TICKET)))
    assert mcp.calls == []
    assert proposal.connector == mcp_tool.CONNECTOR
    assert proposal.resource == mcp_tool.mcp_resource(SERVER_ID, "issue_write")


def test_a_named_call_of_a_tool_that_needs_a_destination_must_name_it(
    factory: Any,  # noqa: F811
) -> None:
    """The document request is a destination-style tool: the model names where, within the connector's pattern."""
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from tests.test_governed_agent_step import (
        PROTECTED,
        PROTECTED_ID,
        RESOURCE,
        _context,
        _manifest,
        _resolver,
    )

    ir = build_ir(parse_manifest(_manifest()), _resolver(), version="1")
    agent = ir.nodes["agent"]
    context = _context(factory)
    request = _request()
    trust = {
        PROTECTED: ToolTrust(PROTECTED, FAIL_CLOSED, False, "dispatch", PROTECTED_ID),
        "list_workdir_files": ToolTrust("list_workdir_files", "read", True, "dispatch"),
    }

    def run(action: ProposedAction) -> Any:
        return record_step_proposal(
            context,
            request,
            answer(request, "proposed_action", usage=USAGE, proposed_actions=(action,)),
            trust,
            bindings=agent.tools,
        )

    recorded = run(named(PROTECTED, {"document_type": "pay_statement"}, resource=RESOURCE))
    assert recorded.state == "proposed"

    with pytest.raises(EffectRefused) as no_destination:
        run(named(PROTECTED, {"document_type": "pay_statement"}))
    assert no_destination.value.code == "resource_not_admitted"
    with pytest.raises(EffectRefused) as wrong_connector:
        run(named(PROTECTED, {"document_type": "x"}, resource=RESOURCE, connector="http"))
    assert wrong_connector.value.code == "claim_mismatch"
    with pytest.raises(EffectRefused) as unknown:
        run(named("nope", {}, resource=RESOURCE))
    assert unknown.value.code == "unknown_tool"
    with pytest.raises(EffectRefused) as read:
        run(named("list_workdir_files", {}, resource=RESOURCE))
    assert read.value.code == "tool_not_protected"


# ----------------------------------------------------- the whole path: a model-style call through a real adapter


def model_call(name: str, args: dict[str, Any], **extra: Any) -> OpenAIChatWorkflowExecutor:
    """The real OpenAI chat adapter over a scripted client whose model calls ``name`` with ``args``: no claim, no connector."""
    message = _OAMsg(tool_calls=[_OAToolCall("call-1", name, json.dumps({"args": args, **extra}))])
    client = _FakeOpenAI([_OAResp(message, _OAUsage(100, 20, 120))])
    return OpenAIChatWorkflowExecutor(
        api_key="unused", default_model="m", client=client, step_clock=lambda: FIXED_NOW
    )


def drive(world: World, executor: Any, **tools: Any) -> Any:
    return execute(world.plan, "go", executor=executor, extra_tools=tools or None)


def test_a_model_call_of_a_registered_write_tool_through_the_real_adapter_is_a_proposal(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    tool = Tool()
    executor = model_call("open_ticket", TICKET)
    result = drive(world, executor, open_ticket=tool)

    proposal, _ = parked(world, result)
    assert tool.calls == []
    assert proposal.resource == tool_resource("open_ticket", "1.0")
    assert proposal.args["arguments"] == TICKET
    # what the model was offered: the tool's arguments and nothing it could claim
    (offered,) = executor._client.chat.completions.calls
    spec = next(t for t in offered["tools"] if t["function"]["name"] == "open_ticket")["function"]
    assert spec["parameters"]["required"] == ["args"]
    assert not {"connector", "connector_version", "effect_class"} & set(
        spec["parameters"]["properties"]
    )


def test_a_model_call_of_an_openapi_write_tool_through_the_real_adapter_sends_nothing(
    factory: Any,  # noqa: F811
) -> None:
    with Server() as server:
        world = _openapi(factory, server.url)
        parked(world, drive(world, model_call("file_ticket", {"body": {"summary": "disk full"}})))
        assert server.requests == []


def test_a_model_call_of_an_mcp_write_tool_through_the_real_adapter_never_reaches_the_server(
    factory: Any,  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    world = _mcp(factory)
    parked(world, drive(world, model_call("write_issue", TICKET)))
    assert mcp.calls == []


def test_a_model_that_volunteers_a_class_through_the_real_adapter_records_nothing(
    factory: Any,  # noqa: F811
) -> None:
    world = _registered(factory)
    tool = Tool()
    result = drive(world, model_call("open_ticket", TICKET, effect_class="read"), open_ticket=tool)
    assert result.status == "error" and "malformed_result" in (result.error or ""), result.error
    assert tool.calls == [] and world.rows() == ([], [])
