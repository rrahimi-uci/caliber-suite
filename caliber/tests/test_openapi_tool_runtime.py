"""Runtime coverage for declarative OpenAPI-backed registered tools."""

from __future__ import annotations

from typing import Any

import httpx

from caliber.egress import EgressPolicy
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.workflow_helpers import make_manifest

#: ``tickets.example.com`` does not resolve, and the executor's pre-check refuses a
#: host it cannot vet. Allowlisting it here is what an operator would do for a real
#: internal host, and it keeps the policy *on* for the rest of the request rather
#: than stubbing the guarded client away — which is how a missing ``policy=``
#: argument previously survived a green suite.
ALLOW_TEST_UPSTREAM = EgressPolicy(
    allowed_hosts=frozenset({"tickets.example.com"}),
    allow_unresolvable_hosts=True,
)


def _ticket_tool_entry() -> ToolRegistryEntry:
    return ToolRegistryEntry(
        name="get_ticket",
        version="1.0",
        module_path="<openapi_http>",
        callable_name="invoke",
        execution_backend="openapi_http",
        backend_config={
            "kind": "openapi_http",
            "method": "GET",
            "path": "/tickets/{ticket_id}",
            "server_url": "https://tickets.example.com",
            "auth_binding": {
                "kind": "bearer",
                "secret_ref": "env://OPENAPI_TICKET_TOKEN",
            },
            "request_content_types": [],
        },
        input_schema={
            "type": "object",
            "properties": {
                "path_params": {
                    "type": "object",
                    "properties": {"ticket_id": {"type": "string"}},
                    "required": ["ticket_id"],
                    "additionalProperties": False,
                }
            },
            "required": ["path_params"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"status_code": {"type": "integer"}},
        },
        side_effect_level="read",
        allow_in_preview=True,
        secret_refs=("env://OPENAPI_TICKET_TOKEN",),
    )


def _ticket_tool_manifest() -> dict[str, Any]:
    manifest = make_manifest("openapi_runtime")
    del manifest["nodes"]["agent"]
    manifest["nodes"]["tool_lookup"] = {
        "id": "tool_lookup",
        "type": "tool",
        "tool_name": "get_ticket",
        "inputs": {"input": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["nodes"]["start"]["outputs"] = {"msg": {"type": "structured"}}
    manifest["tools"] = {
        "get_ticket": {"registry_ref": "tool.get_ticket.v1", "version_constraint": ">=1.0,<2.0"}
    }
    manifest["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]
    return manifest


def test_runtime_executes_openapi_http_registered_tool(monkeypatch) -> None:
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://tickets.example.com/tickets/T-9"
        assert request.headers["Authorization"] == "Bearer ticket-secret"
        return httpx.Response(200, json={"ticket_id": "T-9", "status": "resolved"})

    monkeypatch.setattr(
        "caliber.integrations.openapi.executor.build_client",
        lambda *args, **kwargs: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    resolver = InMemoryToolResolver([_ticket_tool_entry()])
    plan = RuntimePlan(
        ir=build_ir(parse_manifest(_ticket_tool_manifest()), resolver, version="1"),
        resolver=resolver,
        egress_policy=ALLOW_TEST_UPSTREAM,
    )

    result = execute(
        plan,
        {"path_params": {"ticket_id": "T-9"}},
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed"
    assert "resolved" in str(result.output)
    tool_step = next(step for step in result.steps if step.node_id == "tool_lookup")
    assert tool_step.tool_calls[0]["registry_ref"] == "tool.get_ticket.v1"


def test_runtime_openapi_tool_is_blocked_when_the_plan_policy_refuses_the_host(
    monkeypatch,
) -> None:
    """The plan's policy reaches the executor, so a refused host fails the node.

    The counterpart to the test above: same wiring, no allowlist, and no stubbed
    client. It fails if ``_bind`` ever stops threading ``plan.egress_policy`` into
    the executor.
    """

    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "ticket-secret")
    resolver = InMemoryToolResolver([_ticket_tool_entry()])
    plan = RuntimePlan(
        ir=build_ir(parse_manifest(_ticket_tool_manifest()), resolver, version="1"),
        resolver=resolver,
        egress_policy=EgressPolicy(),
    )

    result = execute(
        plan,
        {"path_params": {"ticket_id": "T-9"}},
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "error"
