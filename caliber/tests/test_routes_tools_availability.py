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

from caliber.db.models import CaliberAuditLog, CaliberToolRegistry
from caliber.ids import new_tool_id
from caliber.workflows.builtin_tools import (
    BUILTIN_TOOL_MODULE_BY_NAME,
    BUILTIN_TOOL_NAMES,
    BUILTIN_TOOL_VERSION,
    MEMORY_TOOL_NAMES,
    MEMORY_UNAVAILABLE_REASON,
    RESERVED_NAME_SQUATTER_ACTION,
    RESERVED_TOOL_NAME_CODE,
    RESERVED_TOOL_OWNER_CODE,
    audit_reserved_name_squatters,
    is_builtin_tool,
    register_builtin_tools,
    reserved_tool_name,
    reserved_tool_name_message,
    reserved_tool_owner_message,
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


def _identity(name: str, **overrides: str) -> dict[str, str]:
    """The fields the seed writes for ``name``, with ``overrides`` applied."""
    fields = {
        "name": name,
        "owner": "@caliber",
        "module_path": BUILTIN_TOOL_MODULE_BY_NAME[name],
        "callable_name": name,
        "version": BUILTIN_TOOL_VERSION,
    }
    return {**fields, **overrides}


@pytest.mark.parametrize("name", sorted(BUILTIN_TOOL_NAMES))
def test_the_seeds_own_identity_is_a_built_in(name: str) -> None:
    assert is_builtin_tool(**_identity(name)) is True


@pytest.mark.parametrize(
    "override",
    [
        {"owner": "@someone"},
        {"owner": ""},
        {"module_path": "mycompany.tools"},
        {"callable_name": "lookup_policy"},
        {"callable_name": ""},
        {"version": "2.0"},
        {"version": ""},
    ],
)
def test_a_built_in_needs_every_field_the_seed_writes(override: dict[str, str]) -> None:
    """The legacy squatter: a reserved name and the built-ins' owner are not enough."""
    assert is_builtin_tool(**_identity("grep_files", **override)) is False


def test_a_name_is_trusted_only_with_its_own_module() -> None:
    """Cross-pairing: a built-in name with another built-in's module is not a built-in."""
    for name, module in BUILTIN_TOOL_MODULE_BY_NAME.items():
        for other_module in set(BUILTIN_TOOL_MODULE_BY_NAME.values()) - {module}:
            assert is_builtin_tool(**_identity(name, module_path=other_module)) is False, (
                name,
                other_module,
            )
    assert is_builtin_tool(**{**_identity("grep_files"), "name": "my_tool"}) is False


def test_the_module_map_covers_exactly_the_seeded_names() -> None:
    assert set(BUILTIN_TOOL_MODULE_BY_NAME) == BUILTIN_TOOL_NAMES
    assert BUILTIN_TOOL_MODULE_BY_NAME["extract_document"] == "caliber.workflows.ingestion_tools"
    assert {BUILTIN_TOOL_MODULE_BY_NAME[n] for n in MEMORY_TOOL_NAMES} == {
        "caliber.workflows.memory_tools"
    }


def test_the_seed_writes_exactly_what_the_check_accepts(db_session: Session) -> None:
    register_builtin_tools(db_session)
    rows = db_session.query(CaliberToolRegistry).all()
    assert {row.name for row in rows} == BUILTIN_TOOL_NAMES
    for row in rows:
        assert row.module_path == BUILTIN_TOOL_MODULE_BY_NAME[row.name], row.name
        assert is_builtin_tool(
            name=row.name,
            owner=row.owner,
            module_path=row.module_path,
            callable_name=row.callable_name,
            version=row.version,
        ), row.name


def test_tool_availability_only_ever_downgrades_a_seeded_memory_tool() -> None:
    off = tool_availability(**_identity("memory_list"), memory_enabled=False)
    assert off == ("unavailable", MEMORY_UNAVAILABLE_REASON)
    on = tool_availability(**_identity("memory_list"), memory_enabled=True)
    assert on == ("available", None)
    other = tool_availability(**_identity("grep_files"), memory_enabled=False)
    assert other == ("available", None)
    squatter = tool_availability(
        **_identity("memory_list", callable_name="x"), memory_enabled=False
    )
    assert squatter == ("available", None), (
        "an untrusted row is not a built-in, so it is not downgraded"
    )


def _squatter(
    name: str = "grep_files", version: str = "1.0", **overrides: str
) -> CaliberToolRegistry:
    fields = {
        "tool_id": new_tool_id(),
        "name": name,
        "version": version,
        "description": "registered before the name was reserved",
        "module_path": "caliber.workflows.demo_tools",
        "callable_name": "lookup_policy",
        "owner": "@caliber",
        "visibility": "public",
        "status": "active",
    }
    return CaliberToolRegistry(**{**fields, **overrides})


def test_the_seed_audit_reports_a_squatter_once_and_says_it_blocks_the_seed(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """A squatter at the seed's version silently prevents the real built-in from existing."""
    squatter = _squatter()  # reserved name, built-in owner, but the wrong callable
    db_session.add(squatter)
    db_session.commit()

    with caplog.at_level("ERROR", logger="caliber.workflows.builtin_tools"):
        created = register_builtin_tools(db_session)
        db_session.commit()
        register_builtin_tools(db_session)
        db_session.commit()

    assert created == len(BUILTIN_TOOL_NAMES) - 1, "the squatter's name could not be seeded"
    messages = [r.getMessage() for r in caplog.records if squatter.tool_id in r.getMessage()]
    assert len(messages) == 1, "logged once per process, not on every list call"
    assert "prevents the real built-in from being seeded" in messages[0]
    assert "'grep_files'" in messages[0]
    rows = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == RESERVED_NAME_SQUATTER_ACTION)
        .all()
    )
    assert len(rows) == 1, "one audit record per row, not one per call"
    assert rows[0].entity_id == squatter.tool_id
    assert rows[0].severity == "high"
    assert rows[0].details["blocks_seed"] is True
    assert rows[0].details["callable_name"] == "lookup_policy"
    assert rows[0].details["reserved_for"] == "grep_files"


def test_the_seed_audit_flags_other_versions_and_loose_names_without_claiming_they_block(
    db_session: Session,
) -> None:
    db_session.add_all(
        [
            _squatter(version="2.0", owner="@someone"),
            _squatter(name="Grep_Files", version="1.0", owner="@someone"),
        ]
    )
    db_session.commit()
    found = audit_reserved_name_squatters(db_session)
    db_session.commit()
    assert {(row.name, row.version) for row in found} == {
        ("grep_files", "2.0"),
        ("Grep_Files", "1.0"),
    }
    details = [
        r.details["blocks_seed"]
        for r in db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == RESERVED_NAME_SQUATTER_ACTION)
        .order_by(CaliberAuditLog.log_id)
    ]
    assert details == [False, False]


def test_the_seed_audit_is_silent_for_the_seed_and_for_ordinary_tools(
    db_session: Session,
) -> None:
    register_builtin_tools(db_session)
    db_session.add(_squatter(name="lookup_policy", owner="@someone"))
    db_session.commit()
    assert audit_reserved_name_squatters(db_session) == []
    assert db_session.query(CaliberAuditLog).count() == 0


def test_listing_the_tools_commits_the_squatter_audit_record(
    client: TestClient, db_session: Session
) -> None:
    squatter = _squatter(owner="@someone")
    db_session.add(squatter)
    db_session.commit()
    first = client.get(PREFIX)
    assert first.status_code == 200, first.text
    again = client.get(PREFIX)
    assert again.status_code == 200
    db_session.expire_all()
    rows = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == RESERVED_NAME_SQUATTER_ACTION)
        .all()
    )
    assert [r.entity_id for r in rows] == [squatter.tool_id]
    listed = [r for r in again.json()["data"] if r["tool_id"] == squatter.tool_id]
    assert listed[0]["builtin"] is False


def test_an_edited_built_in_owner_can_be_repaired_but_a_squatter_cannot_take_it(
    client: TestClient, db_session: Session
) -> None:
    rows = {r["name"]: r for r in client.get(PREFIX).json()["data"]}
    seeded = rows["grok_parse"]
    moved = client.patch(f"{PREFIX}/{seeded['tool_id']}", json={"owner": "@someone"})
    assert moved.status_code == 200, moved.text
    listed = {r["name"]: r for r in client.get(PREFIX).json()["data"]}
    assert listed["grok_parse"]["builtin"] is False, "an edited owner makes the row untrusted"

    repaired = client.patch(f"{PREFIX}/{seeded['tool_id']}", json={"owner": "@caliber"})
    assert repaired.status_code == 200, repaired.text
    assert {r["name"]: r for r in client.get(PREFIX).json()["data"]}["grok_parse"][
        "builtin"
    ] is True

    squatter = _squatter(name="grep_files", version="2.0", owner="@someone")
    db_session.add(squatter)
    db_session.commit()
    refused = client.patch(f"{PREFIX}/{squatter.tool_id}", json={"owner": "@caliber"})
    assert refused.status_code == 409, refused.text
    assert RESERVED_TOOL_OWNER_CODE in refused.json()["detail"]
    assert reserved_tool_owner_message() == refused.json()["detail"]


def test_the_audit_flags_a_tool_registered_under_a_connector_name_before_the_rule(
    db_session: Session,
) -> None:
    """A legacy ``document_request`` row is untrusted and reported, not rewritten (B12.3a, #738)."""
    legacy = _squatter(name="document_request", owner="@someone")
    db_session.add(legacy)
    db_session.commit()
    found = audit_reserved_name_squatters(db_session)
    db_session.commit()
    assert [row.tool_id for row in found] == [legacy.tool_id]
    (record,) = db_session.query(CaliberAuditLog).filter(
        CaliberAuditLog.action == RESERVED_NAME_SQUATTER_ACTION
    )
    assert record.details["reserved_for"] == "document_request"
    assert record.details["blocks_seed"] is False
    db_session.expire_all()
    assert db_session.get(type(legacy), legacy.tool_id).owner == "@someone", (
        "the row is not rewritten"
    )
