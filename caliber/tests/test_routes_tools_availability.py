"""The Tools list says which tools CALIBER seeded and which cannot do anything right now (alpha gap P1-21).

A fresh install lists sixteen tools as ``active``. Four of them are the agent-memory tools,
which only return an error dict while ``CALIBER_MEMORY_ENABLED`` is off, and none of them is
marked as one CALIBER put there rather than one an admin registered. A workflow author who
wires in ``memory_search`` learns at run time that it does nothing.

The list and detail routes now carry ``builtin``, ``availability`` and ``availability_reason``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberToolRegistry
from caliber.ids import new_tool_id
from caliber.workflows.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    MEMORY_UNAVAILABLE_REASON,
    RESERVED_TOOL_NAME_CODE,
    is_builtin_tool,
    reserved_tool_name,
    reserved_tool_name_message,
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
    db_session: Session,
) -> None:
    """The label rests on name, owner and module, so a row from before the name was reserved is not one."""
    mine = client.post(PREFIX, json=make_tool_payload("lookup_policy")).json()["data"]
    assert mine["builtin"] is False  # the create response is not annotated
    # Registration now refuses a built-in's name, so the same-named row a registry could already hold
    # (registered before the rule existed) is written directly.
    db_session.add(
        CaliberToolRegistry(
            tool_id=new_tool_id(),
            name="memory_search",
            version="2.0",
            description="registered before the name was reserved",
            module_path="caliber.workflows.demo_tools",
            callable_name="lookup_policy",
            owner="@someone",
            visibility="public",
            status="active",
        )
    )
    db_session.commit()

    rows = client.get(PREFIX).json()["data"]
    registered = [r for r in rows if r["version"] == "2.0" or r["name"] == "lookup_policy"]
    assert len(registered) == 2
    for row in registered:
        assert row["builtin"] is False, row["name"]
        assert row["availability"] == "available", row["name"]


def test_the_same_name_cannot_be_registered_now_that_it_is_reserved(client: TestClient) -> None:
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
    assert spoof.status_code == 409, spoof.text
    assert "reserved_tool_name" in spoof.text
    rows = client.get(PREFIX).json()["data"]
    assert [r["version"] for r in rows if r["name"] == "memory_search"] == ["1.0"]


@pytest.mark.parametrize(
    ("name", "builtin"),
    [
        ("grep_files", "grep_files"),
        ("memory_add", "memory_add"),
        ("extract_document", "extract_document"),
        ("  Grep_Files ", "grep_files"),
        ("tool.read_text_file.v3", "read_text_file"),
        ("sandbox_python.v1", "sandbox_python"),
        ("create_artifact", "create_artifact"),
    ],
)
def test_reserved_tool_name_finds_the_built_in_a_name_collides_with(
    name: str, builtin: str
) -> None:
    assert reserved_tool_name(name) == builtin
    message = reserved_tool_name_message(name, builtin)
    assert message.startswith(f"{RESERVED_TOOL_NAME_CODE}: ")
    assert repr(name) in message
    assert repr(builtin) in message


@pytest.mark.parametrize(
    "name",
    ["", " ", "lookup_policy", "grep_files_v2", "my_grep_files", "grep-files", "tool.", "memory"],
)
def test_reserved_tool_name_leaves_every_other_name_alone(name: str) -> None:
    assert reserved_tool_name(name) is None


def test_every_seeded_name_is_reserved() -> None:
    assert all(reserved_tool_name(name) == name for name in BUILTIN_TOOL_NAMES)


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
