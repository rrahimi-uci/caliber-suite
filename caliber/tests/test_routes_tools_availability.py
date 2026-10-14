"""The Tools list says which tools CALIBER seeded and which cannot do anything right now (alpha gap P1-21).

A fresh install lists sixteen tools as ``active``. Four of them are the agent-memory tools,
which only return an error dict while ``CALIBER_MEMORY_ENABLED`` is off, and none of them is
marked as one CALIBER put there rather than one an admin registered. A workflow author who
wires in ``memory_search`` learns at run time that it does nothing.

The list and detail routes now carry ``builtin``, ``availability`` and ``availability_reason``.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.workflows.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    MEMORY_UNAVAILABLE_REASON,
    is_builtin_tool,
    tool_availability,
)
from tests.workflow_helpers import make_tool_payload

PREFIX = "/ajax-api/2.0/mlflow/caliber/tools"


def _by_name(client: TestClient) -> dict[str, dict[str, object]]:
    response = client.get(PREFIX)
    assert response.status_code == 200, response.text
    return {row["name"]: row for row in response.json()["data"]}


def test_a_fresh_install_marks_every_seeded_tool_as_built_in(client: TestClient) -> None:
    rows = _by_name(client)
    assert set(rows) == BUILTIN_TOOL_NAMES
    assert all(row["builtin"] is True for row in rows.values())


def test_memory_tools_are_unavailable_while_memory_is_off(client: TestClient) -> None:
    rows = _by_name(client)
    for name in MEMORY_TOOL_NAMES:
        assert rows[name]["availability"] == "unavailable", name
        assert rows[name]["availability_reason"] == MEMORY_UNAVAILABLE_REASON
    assert "CALIBER_MEMORY_ENABLED" in str(rows["memory_search"]["availability_reason"])
    # Everything else the install seeds works without memory.
    for name, row in rows.items():
        if name not in MEMORY_TOOL_NAMES:
            assert row["availability"] == "available", name
            assert row["availability_reason"] is None, name


def test_memory_tools_are_available_once_memory_is_on(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(update={"memory_enabled": True})
    rows = _by_name(client)
    for name in MEMORY_TOOL_NAMES:
        assert rows[name]["availability"] == "available", name
        assert rows[name]["availability_reason"] is None
        assert rows[name]["builtin"] is True


def test_the_detail_route_carries_the_same_flags(client: TestClient) -> None:
    rows = _by_name(client)
    detail = client.get(f"{PREFIX}/{rows['memory_add']['tool_id']}")
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["builtin"] is True
    assert data["availability"] == "unavailable"
    assert data["availability_reason"] == MEMORY_UNAVAILABLE_REASON

    sandbox = client.get(f"{PREFIX}/{rows['sandbox_python']['tool_id']}").json()["data"]
    assert sandbox["builtin"] is True
    assert sandbox["availability"] == "available"


def test_a_registered_tool_is_not_built_in_and_cannot_pass_itself_off_as_one(
    client: TestClient,
) -> None:
    """An owner is free text on registration, so the label rests on name, owner and module."""
    mine = client.post(PREFIX, json=make_tool_payload("lookup_policy")).json()["data"]
    assert mine["builtin"] is False  # the create response is not annotated
    spoof = client.post(
        PREFIX,
        json=make_tool_payload(
            "memory_search",
            version="2.0",
            owner="@caliber",
            module_path="caliber.workflows.demo_tools",
            callable_name="lookup_policy",
        ),
    )
    assert spoof.status_code in (200, 201), spoof.text

    rows = client.get(PREFIX).json()["data"]
    registered = [r for r in rows if r["version"] == "2.0" or r["name"] == "lookup_policy"]
    assert len(registered) == 2
    for row in registered:
        assert row["builtin"] is False, row["name"]
        assert row["availability"] == "available", row["name"]


@pytest.mark.parametrize(
    ("name", "owner", "module", "expected"),
    [
        ("sandbox_python", "@caliber", "caliber.workflows.demo_tools", True),
        ("extract_document", "@caliber", "caliber.workflows.ingestion_tools", True),
        ("memory_add", "@caliber", "caliber.workflows.memory_tools", True),
        ("sandbox_python", "@someone", "caliber.workflows.demo_tools", False),
        ("sandbox_python", "@caliber", "mycompany.tools", False),
        ("my_tool", "@caliber", "caliber.workflows.demo_tools", False),
    ],
)
def test_is_builtin_tool_needs_name_owner_and_module(
    name: str, owner: str, module: str, expected: bool
) -> None:
    assert is_builtin_tool(name=name, owner=owner, module_path=module) is expected


def test_tool_availability_only_ever_downgrades_a_seeded_memory_tool() -> None:
    off = tool_availability(
        name="memory_list",
        owner="@caliber",
        module_path="caliber.workflows.memory_tools",
        memory_enabled=False,
    )
    assert off == ("unavailable", MEMORY_UNAVAILABLE_REASON)
    on = tool_availability(
        name="memory_list",
        owner="@caliber",
        module_path="caliber.workflows.memory_tools",
        memory_enabled=True,
    )
    assert on == ("available", None)
    other = tool_availability(
        name="grep_files",
        owner="@caliber",
        module_path="caliber.workflows.demo_tools",
        memory_enabled=False,
    )
    assert other == ("available", None)
