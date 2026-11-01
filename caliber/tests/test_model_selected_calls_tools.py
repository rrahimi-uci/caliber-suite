"""B04.6 (#768): the OpenAPI and MCP tools of an agent loop, classified like a workflow tool node's.

A model-chosen OpenAPI call is classified by the method its registry row's own configuration selects (a pack's operation is
chosen by the call), an MCP call by the server record and the administrator's tool policy for exactly that tool. A read runs
inline; a protected call is refused before a request is built or a server is reached.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.mcp_policy import TOOL_UNCLASSIFIED
from caliber.workflows.tools import ToolRegistryEntry
from tests.mcp_fakes import FakeMcp, seed_server, server_policy
from tests.test_model_selected_calls import (
    SENTINEL,
    World,
    agent_manifest,
    chat_loop,
    factory,  # noqa: F401 - fixtures
    mcp,  # noqa: F401
    mcp_binding,
    refused,
    registered,
)
from tests.test_openapi_governed_dispatch import Server, config, entry_for
from tests.test_openapi_governed_dispatch import seed_row as seed_openapi_row

pytestmark = pytest.mark.usefixtures("fixed_step_clock")


def _openapi_world(
    factory: sessionmaker[Session],  # noqa: F811
    url: str,
    **plan_fields: Any,
) -> tuple[World, ToolRegistryEntry, ToolRegistryEntry]:
    read = entry_for("list_tickets", "read", config(url, method="GET", idempotency_key_header=None))
    write = entry_for("file_ticket", "write", config(url))
    seed_openapi_row(factory, read)
    seed_openapi_row(factory, write)
    manifest = agent_manifest(
        {"list_tickets": registered("list_tickets"), "file_ticket": registered("file_ticket")}
    )
    return World(factory, manifest, [read, write], **plan_fields), read, write


def test_an_openapi_read_runs_in_the_loop_and_a_write_is_refused_before_anything_is_sent(
    factory: sessionmaker[Session],  # noqa: F811
) -> None:
    with Server() as server:
        world, _, _ = _openapi_world(factory, server.url)
        result, _ = chat_loop(
            world,
            ("list_tickets", {"query_params": {"q": "open"}}),
            ("file_ticket", {"body": {"summary": SENTINEL}}),
        )
        seen = world.tool_results(result)
        assert seen["file_ticket"] == refused("file_ticket")
        assert isinstance(seen["list_tickets"], dict)
        assert seen["list_tickets"]["status_code"] == 201
        assert [(r["method"], r["path"].split("?")[0]) for r in server.requests] == [
            ("GET", "/tickets")
        ]
    assert world.rows() == ([], [])


def test_an_openapi_packs_operation_decides_the_class_of_each_call(
    factory: sessionmaker[Session],  # noqa: F811
) -> None:
    with Server() as server:
        pack = {
            "kind": "openapi_http_pack",
            "server_url": server.url,
            "auth_binding": None,
            "max_attempts": 1,
            "operations": [
                {"operation_key": "list", "method": "GET", "path": "/tickets"},
                {
                    "operation_key": "create",
                    "method": "POST",
                    "path": "/tickets",
                    "request_content_types": ["application/json"],
                },
            ],
        }
        entry = entry_for("tickets", "read", pack)
        seed_openapi_row(factory, entry)
        world = World(factory, agent_manifest({"tickets": registered("tickets")}), [entry])
        result, _ = chat_loop(
            world,
            ("tickets", {"operation": "list"}),
            ("tickets", {"operation": "create", "body": {"summary": SENTINEL}}),
        )
        (step,) = [s for s in result.steps if s.node_id == "agent"]
        outcomes = [call["result"] for call in step.tool_calls]
        assert isinstance(outcomes[0], dict) and outcomes[0]["status_code"] == 201
        assert outcomes[1] == refused("tickets")
        assert [r["method"] for r in server.requests] == ["GET"]


def test_an_openapi_call_whose_operation_cannot_be_resolved_is_refused_with_the_preflight_code(
    factory: sessionmaker[Session],  # noqa: F811
) -> None:
    with Server() as server:
        pack = {**config(server.url), "kind": "openapi_http_pack", "operations": []}
        entry = entry_for("tickets", "read", pack)
        seed_openapi_row(factory, entry)
        world = World(factory, agent_manifest({"tickets": registered("tickets")}), [entry])
        result, _ = chat_loop(world, ("tickets", {"operation": "missing"}))
        assert world.tool_results(result)["tickets"] == refused(
            "tickets", "openapi_operation_unresolved"
        )
        assert server.requests == []


def test_an_openapi_binding_that_is_not_its_row_is_refused_even_for_a_read(
    factory: sessionmaker[Session],  # noqa: F811
) -> None:
    with Server() as server:
        read = entry_for("list_tickets", "read", config(server.url, method="GET"))
        seed_openapi_row(factory, read, row_config=config(server.url, method="GET", path="/other"))
        world = World(factory, agent_manifest({"list_tickets": registered("list_tickets")}), [read])
        result, _ = chat_loop(world, ("list_tickets", {}))
        assert world.tool_results(result)["list_tickets"] == refused(
            "list_tickets", "registry_row_mismatch"
        )
        assert server.requests == []


def _mcp_world(factory: sessionmaker[Session], **plan_fields: Any) -> World:  # noqa: F811
    seed_server(factory, tools={"mystery": None, "danger": server_policy("external_action")})
    manifest = agent_manifest(
        {
            "lookup_docs": mcp_binding("lookup"),
            "write_issue": mcp_binding("issue_write"),
            "mystery": mcp_binding("mystery"),
        }
    )
    return World(factory, manifest, [], **plan_fields)


def test_an_mcp_read_runs_in_the_loop_and_a_protected_tool_never_reaches_the_server(
    factory: sessionmaker[Session],  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    world = _mcp_world(factory)
    result, _ = chat_loop(
        world,
        ("lookup_docs", {"query": "refund"}),
        ("write_issue", {"title": SENTINEL}),
        ("mystery", {}),
    )
    seen = world.tool_results(result)
    assert seen["write_issue"] == refused("write_issue")
    assert seen["mystery"] == refused("mystery", TOOL_UNCLASSIFIED)
    assert [c["name"] for c in mcp.calls] == ["lookup"], (
        "only the read-class tool reached the server"
    )
    assert world.rows() == ([], [])


def test_the_development_profile_runs_a_protected_mcp_tool_in_the_loop_as_before(
    factory: sessionmaker[Session],  # noqa: F811
    mcp: FakeMcp,  # noqa: F811
) -> None:
    world = _mcp_world(factory, legacy_guarded_effects=True)
    result, _ = chat_loop(world, ("write_issue", {"title": "x"}))
    assert world.tool_results(result)["write_issue"]["tool_name"] == "issue_write"
    assert [c["name"] for c in mcp.calls] == ["issue_write"]
