"""B04.3 (#741): the registered-tool row of the effect-path matrix.

Every tool a workflow can name is routed from its registry identity: inline (read), through the dispatcher, or refused.
The matrix is built from the seeded built-ins and the protected-effect inventory, so a built-in added later without a
decision, or a trust rule loosened, fails here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberToolRegistry
from caliber.effects.connectors.openapi_http import OpenApiOperation
from caliber.effects.dispatcher import EffectRefused, dispatch
from caliber.effects.inventory import (
    EFFECT_CLASSES,
    FAIL_CLOSED,
    effect_class,
    tool_disposition,
)
from caliber.effects.registered_tools import (
    DISPATCH,
    INLINE,
    REFUSE,
    RegisteredToolIdentity,
    load_registered_tool_identity,
    route_openapi_tool,
    route_registered_tool,
    tool_intent_args,
    tool_resource,
)
from caliber.workflows.builtin_tools import BUILTIN_TOOL_NAMES, register_builtin_tools
from tests.test_openapi_governed_dispatch import (
    OpenApiRun,
    Server,
    config,
    entry_for,
)
from tests.test_openapi_governed_dispatch import verify as openapi_verify
from tests.test_registered_tool_dispatch import (
    Run,
    Tool,
    _world,
    leased,
    proposals,
    seed_row,
    tool_entry,
    verifier,
)

DECLARED_CLASS = {
    "read": "read",
    "write": "reversible_write",
    "external_action": "irreversible_write",
}


def _identity(**overrides: Any) -> RegisteredToolIdentity:
    fields: dict[str, Any] = {
        "name": "open_ticket",
        "owner": "@ops",
        "version": "1.0",
        "module_path": "ops.tools",
        "callable_name": "open_ticket",
        "side_effect_level": "write",
    }
    fields.update(overrides)
    return RegisteredToolIdentity(**fields)


@pytest.fixture
def factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with _world("sqlite", tmp_path, monkeypatch) as factory:
        yield factory


@pytest.fixture
def seeded_identities(db_session: Session) -> dict[str, RegisteredToolIdentity]:
    register_builtin_tools(db_session)
    db_session.commit()
    found = {
        name: load_registered_tool_identity(db_session, name=name, version="1.0")
        for name in sorted(BUILTIN_TOOL_NAMES)
    }
    assert all(identity is not None for identity in found.values())
    return found  # type: ignore[return-value]


@pytest.mark.parametrize("name", sorted(BUILTIN_TOOL_NAMES))
def test_every_seeded_builtin_is_inline_dispatched_or_refused_as_the_inventory_decided(
    seeded_identities: dict[str, RegisteredToolIdentity], name: str
) -> None:
    identity = seeded_identities[name]
    route = route_registered_tool(identity)

    assert route.trusted is True
    if tool_disposition(name, caliber_defined=True) == "refuse":
        assert route.route == REFUSE
        return
    floor = EFFECT_CLASSES.index(DECLARED_CLASS[identity.side_effect_level])
    strictest = EFFECT_CLASSES[
        max(EFFECT_CLASSES.index(effect_class(name, caliber_defined=True)), floor)
    ]
    assert route.effect_class == strictest
    assert route.route == (INLINE if strictest == "read" else DISPATCH)


@pytest.mark.parametrize("name", sorted(BUILTIN_TOOL_NAMES))
@pytest.mark.parametrize("change", ["owner", "module_path", "callable_name", "version"])
def test_a_row_that_is_not_the_seeds_own_is_never_trusted_inline_or_refused_by_name(
    seeded_identities: dict[str, RegisteredToolIdentity], name: str, change: str
) -> None:
    """Any drift from the seed makes the row an impostor: fail-closed class, dispatched, never inline."""
    identity = replace(
        seeded_identities[name], **{change: "x" + getattr(seeded_identities[name], change)}
    )

    route = route_registered_tool(identity)

    assert route.trusted is False
    assert route.route == DISPATCH
    assert route.effect_class == FAIL_CLOSED


@pytest.mark.parametrize(
    ("level", "route", "klass"),
    [
        ("read", INLINE, "read"),
        ("write", DISPATCH, "reversible_write"),
        ("external_action", DISPATCH, "irreversible_write"),
        ("something_else", DISPATCH, FAIL_CLOSED),
        ("", DISPATCH, FAIL_CLOSED),
    ],
)
def test_an_administrator_registered_tool_is_routed_by_its_own_rows_level(
    level: str, route: str, klass: str
) -> None:
    decided = route_registered_tool(_identity(side_effect_level=level))

    assert (decided.route, decided.effect_class, decided.trusted) == (route, klass, False)


def test_a_builtin_whose_row_declares_a_stricter_level_than_the_inventory_takes_the_stricter(
    seeded_identities: dict[str, RegisteredToolIdentity],
) -> None:
    """The level is not part of the seed's identity, so an edited level can only make a built-in stricter."""
    edited = replace(seeded_identities["list_workdir_files"], side_effect_level="external_action")

    decided = route_registered_tool(edited)

    assert (decided.route, decided.effect_class, decided.trusted) == (
        DISPATCH,
        "irreversible_write",
        True,
    )


def test_an_unreadable_tool_is_dispatched_fail_closed() -> None:
    decided = route_registered_tool(None)

    assert (decided.route, decided.effect_class, decided.trusted) == (DISPATCH, FAIL_CLOSED, False)


@pytest.mark.parametrize(
    "name", ["Grep_Files", " list_workdir_files ", "tool.read_workdir_file.v2"]
)
def test_a_custom_tool_named_like_a_builtin_is_fail_closed_even_when_it_declares_read(
    name: str,
) -> None:
    decided = route_registered_tool(_identity(name=name, side_effect_level="read"))

    assert (decided.route, decided.effect_class) == (DISPATCH, FAIL_CLOSED)


def test_loading_an_identity_that_is_not_registered_answers_none(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        assert load_registered_tool_identity(session, name="nope", version="1.0") is None


def test_the_proposal_target_and_intent_name_the_tool_and_its_arguments() -> None:
    assert tool_resource("open_ticket", "1.0") == "registered-tool/open_ticket/1.0"
    assert tool_intent_args(
        name="open_ticket", version="1.0", arguments={"a": 1}, input_text="hi"
    ) == {"tool": "open_ticket", "version": "1.0", "arguments": {"a": 1}, "input": "hi"}


def test_registered_write_tool_requires_proposal(factory: sessionmaker[Session]) -> None:
    """A write-class registered tool never runs without an approved proposal.

    Through the runtime the missing approval is a refusal before the tool is reached; the dispatcher then refuses the
    very proposal the runtime recorded with ``proposal_not_approved``, and the tool has still not run.
    """
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, entry)
    tool = Tool()
    run = Run(factory, entry, tool, authorize=lambda _proposal: None)

    result = run.execute()

    assert result.status == "error"
    assert "authorization was not granted" in (result.error or "")
    (recorded,) = proposals(factory)
    assert recorded.state == "proposed"
    with factory() as session:
        lease = leased(session, "runtime-owner")
        session.commit()
    with pytest.raises(EffectRefused) as refused:
        dispatch(
            factory,
            recorded.proposal_id,
            approval_id=None,
            lease=lease,
            backend_kind="registered_tool",
            backend_config={"invoker": tool, "receipt_verifier": verifier},
            approval_validator=_canonical_validator(),
        )
    assert refused.value.code == "proposal_not_approved"
    assert tool.calls == []


def _canonical_validator() -> Any:
    import caliber.workflows.runtime as runtime_module

    return runtime_module.approval_valid_at_commit


def test_the_matrix_covers_every_registry_row_shape(db_session: Session) -> None:
    """Both halves of the registry — seeded built-ins and administrator rows — are decided by the same function."""
    register_builtin_tools(db_session)
    db_session.add(
        CaliberToolRegistry(
            tool_id="tool-matrix",
            name="custom_lookup",
            version="1.0",
            module_path="ops.tools",
            callable_name="custom_lookup",
            side_effect_level="read",
            owner="@ops",
        )
    )
    db_session.commit()
    custom = load_registered_tool_identity(db_session, name="custom_lookup", version="1.0")
    assert custom is not None
    assert route_registered_tool(custom).route == INLINE


# ---------------------------------------------------------------- B04.4 (#750): registered OpenAPI tools


def _openapi_identity(**overrides: Any) -> RegisteredToolIdentity:
    return _identity(
        module_path="<openapi_http>",
        callable_name="invoke",
        execution_backend="openapi_http",
        **overrides,
    )


_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@pytest.mark.parametrize("method", [*_SAFE_METHODS, *_UNSAFE_METHODS, "get", "Post", "TRACE", ""])
@pytest.mark.parametrize("level", ["read", "write", "external_action", "something_else", ""])
def test_an_openapi_call_is_routed_by_the_stricter_of_its_rows_level_and_its_method(
    level: str, method: str
) -> None:
    decided = route_openapi_tool(_openapi_identity(side_effect_level=level), method=method)

    safe = method.upper() in _SAFE_METHODS
    declared = DECLARED_CLASS.get(level, FAIL_CLOSED)
    expected = max(declared, "read" if safe else "reversible_write", key=EFFECT_CLASSES.index)
    assert decided.effect_class == expected
    assert decided.route == (INLINE if expected == "read" else DISPATCH)
    assert decided.trusted is False, "a registered OpenAPI tool is never a CALIBER built-in"
    if not safe:
        assert decided.route == DISPATCH, "no method but a read method is ever inline"


@pytest.mark.parametrize("identity", [None, _identity()], ids=["no-row", "python-row"])
def test_an_openapi_call_with_no_openapi_row_cannot_be_proposed_and_is_refused(
    identity: RegisteredToolIdentity | None,
) -> None:
    decided = route_openapi_tool(identity, method="GET")

    assert (decided.route, decided.effect_class, decided.trusted) == (REFUSE, FAIL_CLOSED, False)


def test_an_openapi_tool_named_like_a_builtin_is_fail_closed_even_for_a_get_declared_read() -> None:
    decided = route_openapi_tool(
        _openapi_identity(name="grep_files", side_effect_level="read"), method="GET"
    )

    assert (decided.route, decided.effect_class) == (DISPATCH, FAIL_CLOSED)


def test_a_refused_builtin_identity_on_an_openapi_row_is_refused_not_dispatched(
    seeded_identities: dict[str, RegisteredToolIdentity],
) -> None:
    """A row that is the seed's own `read_text_file` in every field (inventory disposition `refuse`) is never proposed."""
    identity = replace(seeded_identities["read_text_file"], execution_backend="openapi_http")

    decided = route_openapi_tool(identity, method="GET")

    assert (decided.route, decided.trusted) == (REFUSE, True)


def test_a_refused_builtin_name_squatting_on_an_openapi_row_is_never_inline() -> None:
    """The reserved-name rule is the registered-tool one: an impostor row is fail-closed, whatever its method."""
    decided = route_openapi_tool(
        _openapi_identity(name="read_text_file", side_effect_level="read"), method="GET"
    )

    assert decided.route == DISPATCH


def test_loading_an_openapi_row_reads_its_own_configuration_and_schema(
    factory: sessionmaker[Session],
) -> None:
    config_ = {"kind": "openapi_http", "method": "POST", "path": "/t", "server_url": "http://x"}
    schema = {"type": "object"}
    with factory() as session:
        session.add(
            CaliberToolRegistry(
                tool_id="tool-openapi-row",
                name="open_ticket",
                version="1.0",
                module_path="<openapi_http>",
                callable_name="invoke",
                execution_backend="openapi_http",
                backend_config=config_,
                input_schema=schema,
                side_effect_level="write",
                owner="@ops",
            )
        )
        session.commit()
        identity = load_registered_tool_identity(session, name="open_ticket", version="1.0")

    assert identity is not None
    assert (identity.backend_config, identity.input_schema) == (config_, schema)
    assert identity.execution_backend == "openapi_http"


def test_openapi_non_get_requires_proposal(factory: sessionmaker[Session]) -> None:
    """A non-``GET`` OpenAPI call never reaches the downstream without an approved proposal.

    Through the runtime the missing approval is a refusal before anything is sent; the dispatcher then refuses the very
    proposal the runtime recorded with ``proposal_not_approved``, and the downstream has still not been called. A ``GET``
    on a read row is the one call that runs without one.
    """
    with Server() as server:
        entry = entry_for("open_ticket", "read", config(server.url))  # under-declared: a POST
        run = OpenApiRun(factory, entry, authorize=lambda _proposal: None)

        result = run.execute()

        assert result.status == "error"
        assert "authorization was not granted" in (result.error or "")
        (recorded,) = proposals(factory)
        assert recorded.state == "proposed"
        with factory() as session:
            lease = leased(session, "runtime-owner")
            session.commit()
        operation = OpenApiOperation("open_ticket", "1.0", config(server.url))
        with pytest.raises(EffectRefused) as refused:
            dispatch(
                factory,
                recorded.proposal_id,
                approval_id=None,
                lease=lease,
                backend_kind="openapi_http",
                backend_config={"operation": operation, "receipt_verifier": openapi_verify},
                approval_validator=_canonical_validator(),
            )
        assert refused.value.code == "proposal_not_approved"
        assert server.requests == []

        reading = entry_for(
            "get_ticket",
            "read",
            config(server.url, method="GET", path="/tickets/T-1", idempotency_key_header=None),
        )
        got = OpenApiRun(factory, reading).execute({"query_params": {"x": "1"}})
        assert got.status == "completed", got.error
        assert [r["method"] for r in server.requests] == ["GET"]


#: Every production module that names the PUBLIC OpenAPI entry (``execute_openapi_http_tool``, ``bind_openapi_http_tool``),
#: and why it may. The public entry sends only a read (``GET``, ``HEAD``, ``OPTIONS``) and refuses any other method with
#: ``unsupported_effect_path`` (B04.4, #750), so none of these can perform a write. A new caller fails this test until it is
#: routed through the dispatcher or deliberately pinned here with its reason.
RAW_OPENAPI_CALLERS = {
    "assistant/capabilities.py": "Aria's published-tool call (EP-034): reads only, a protected method is refused by the entry; its governed write route is #1063",
    "effects/reconcile.py": "the reconciler's lookup by key: a GET that carries no effect",
    "integrations/openapi/executor.py": "the executor itself",
    "routes/openapi_integrations.py": "the operator's draft preview (EP-033) in the reference profile: reads only, a protected method is refused by the entry (#1072)",
    "routes/system_effects.py": "the operator's lookup by key: a GET that carries no effect",
    "workflows/runtime.py": "inline reads of OpenAPI tool nodes; model-chosen calls (B04.6, #768) and exports (#1051) can read but not write",
}

#: Every production module that names the PRIVATE raw transport (``_send_raw``), which sends any method with no governance
#: of its own, and why it may. It is the only door through which a write reaches the network, so the set is deliberately
#: tiny and a new name here is a design decision.
RAW_TRANSPORT_CALLERS = {
    "effects/dispatcher.py": "the private connector backends (document request, OpenAPI): the dispatcher's own transport",
    "integrations/openapi/executor.py": "defines it; the public entry delegates to it only after refusing a protected method",
}


#: Every production module that names the DEVELOPMENT-PROFILE legacy entry (``bind_legacy_openapi_http_tool``): it sends any method with no proposal, approval, lease or receipt, for a plan outside the reference
#: profile with no governed context (#1068). Not the governed path; the runtime binds it only for such a plan.
LEGACY_OPENAPI_CALLERS = {
    "integrations/openapi/executor.py": "defines it, over the private raw transport",
    "workflows/runtime.py": "binds it only for a plan with legacy_guarded_effects and no governed context (development profile)",
}


#: Every production module that names the DEVELOPMENT-PROFILE draft-preview entry (``execute_legacy_openapi_draft_preview``): it sends any method for the operator's
#: draft preview (EP-033) with no proposal, approval, lease or receipt. Only the preview route may, and it picks it only outside the reference profile (#1072).
LEGACY_PREVIEW_CALLERS = {
    "integrations/openapi/executor.py": "defines it, over the private raw transport",
    "routes/openapi_integrations.py": "the draft preview route, only outside the reference profile (development: exempt, gated by allow_in_preview)",
}


def _names_the_raw_executor(node: Any, raw: set[str]) -> bool:
    import ast

    if isinstance(node, ast.Name):
        return node.id in raw
    if isinstance(node, ast.Attribute):
        return node.attr in raw
    if isinstance(node, ast.alias):
        return node.name in raw
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name in raw
    if isinstance(node, ast.Constant):
        # ``getattr(executor, "_send_raw")``, ``vars(executor)["_send_raw"]``, ``importlib`` style access. Only an exact
        # string is seen: a name assembled from pieces is not (a docstring that merely mentions it is a longer string).
        return isinstance(node.value, str) and node.value in raw
    return False


def _modules_naming(raw: set[str]) -> set[str]:
    import ast
    from pathlib import Path

    import caliber

    root = Path(caliber.__file__).resolve().parent
    return {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if any(
            _names_the_raw_executor(node, raw)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }


def test_the_raw_openapi_executor_is_named_only_by_pinned_callers() -> None:
    found = _modules_naming({"execute_openapi_http_tool", "bind_openapi_http_tool"})
    assert found == set(RAW_OPENAPI_CALLERS)


# ---------------------------------------------------------------- B04.5 (#759): MCP tools


def _mcp_server(**overrides: Any) -> Any:
    from caliber.mcp_gateway import McpServerConfig

    fields: dict[str, Any] = {
        "server_id": "MCP-matrix",
        "name": "matrix",
        "transport": "sse",
        "uri": "https://mcp.example.test/mcp",
        "command": "",
        "args": (),
        "env": {},
        "headers": {},
        "auth_type": "none",
        "auth_config": {},
        "status": "active",
        "discovered_tools": ({"name": "t"},),
        "tool_policies": {
            "t": {"allowed": True, "side_effect_level": "write", "requires_approval": False}
        },
    }
    fields.update(overrides)
    return McpServerConfig(**fields)


@pytest.fixture
def mcp_world(monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.mcp_fakes import FakeMcp

    return FakeMcp().install(monkeypatch)


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reference profile: where the gateway refuses a protected call with no proposal and EP-114 is refused."""
    monkeypatch.setenv("CALIBER_PROFILE", "reference")


def test_the_mcp_route_names_are_the_registered_tool_route_names() -> None:
    from caliber.effects import mcp_tools

    assert (mcp_tools.INLINE, mcp_tools.DISPATCH, mcp_tools.REFUSE) == (INLINE, DISPATCH, REFUSE)


@pytest.mark.parametrize(
    ("level", "route", "klass"),
    [
        ("read", INLINE, "read"),
        ("write", DISPATCH, "reversible_write"),
        ("external_action", DISPATCH, "irreversible_write"),
    ],
)
def test_an_mcp_tool_is_routed_by_the_administrators_level_for_exactly_that_tool(
    mcp_world: Any, level: str, route: str, klass: str
) -> None:
    from caliber.effects.mcp_tools import route_mcp_tool

    server = _mcp_server(
        tool_policies={
            "t": {"allowed": True, "side_effect_level": level, "requires_approval": False}
        }
    )

    decided = route_mcp_tool(server, "t")

    assert (decided.route, decided.effect_class, decided.code) == (route, klass, None)
    assert decided.digest is not None and decided.policy_refused is False


def _policy(**overrides: Any) -> dict[str, Any]:
    return {
        "t": {"allowed": True, "side_effect_level": "read", "requires_approval": False} | overrides
    }


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"discovered_tools": ()}, "mcp_tools_not_discovered"),
        ({"discovered_tools": ({"name": "other"},)}, "mcp_tool_not_discovered"),
        ({"tool_policies": {}}, "mcp_tool_unclassified"),
        ({"tool_policies": {"t": {"allowed": True}}}, "mcp_tool_unclassified"),
        ({"tool_policies": _policy(allowed="yes")}, "mcp_tool_unclassified"),
        ({"tool_policies": _policy(requires_approval=1)}, "mcp_tool_unclassified"),
        ({"tool_policies": _policy(side_effect_level="danger")}, "mcp_tool_unclassified"),
        ({"tool_policies": _policy(allowed=False)}, "mcp_tool_not_allowed"),
        ({"status": "disabled"}, "mcp_server_not_ready"),
        ({"transport": "carrier-pigeon"}, "mcp_server_not_ready"),
    ],
)
def test_an_mcp_tool_nobody_classified_or_allowed_is_refused_with_the_gates_code(
    mcp_world: Any, overrides: dict[str, Any], code: str
) -> None:
    from caliber.effects.mcp_tools import route_mcp_tool

    decided = route_mcp_tool(_mcp_server(**overrides), "t")

    assert (decided.route, decided.effect_class, decided.code) == (REFUSE, FAIL_CLOSED, code)
    assert decided.policy_refused is True and decided.digest is None


def test_the_servers_own_claims_never_change_the_route(mcp_world: Any) -> None:
    from caliber.effects.mcp_tools import route_mcp_tool

    claims = {"name": "t", "annotations": {"readOnlyHint": True}, "description": "read only"}
    server = _mcp_server(discovered_tools=(claims,))

    assert route_mcp_tool(server, "t").route == DISPATCH


def _first_party(**overrides: Any) -> Any:
    import sys

    return _mcp_server(
        transport="stdio",
        uri="",
        command=sys.executable,
        args=("-m", "caliber.mcp_servers.db", "--mode", "relational"),
        **overrides,
    )


def _policies(*names: str, level: str) -> dict[str, Any]:
    return {
        "discovered_tools": tuple({"name": name} for name in names),
        "tool_policies": {
            name: {"allowed": True, "side_effect_level": level, "requires_approval": False}
            for name in names
        },
    }


@pytest.mark.usefixtures("reference")
def test_every_first_party_database_write_tool_is_refused_whatever_its_policy_says(
    mcp_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from caliber.effects.mcp_tools import INVENTORY_REFUSAL, route_mcp_tool
    from tests.test_protected_effect_inventory import db_mcp_write_tools

    monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", sys.executable)
    writes = sorted(db_mcp_write_tools())
    assert writes
    server = _first_party(**_policies(*writes, level="read"))

    for name in writes:
        decided = route_mcp_tool(server, name)
        assert (decided.route, decided.code, decided.first_party) == (
            REFUSE,
            INVENTORY_REFUSAL,
            True,
        ), name
        assert decided.policy_refused is False


def test_a_first_party_database_read_tool_follows_its_administrators_policy(
    mcp_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from caliber.effects.mcp_tools import route_mcp_tool
    from tests.test_protected_effect_inventory import DB_MCP_READ_TOOLS

    monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", sys.executable)
    reading = _first_party(**_policies(*DB_MCP_READ_TOOLS, level="read"))
    writing = _first_party(**_policies(*DB_MCP_READ_TOOLS, level="write"))

    for name in DB_MCP_READ_TOOLS:
        assert route_mcp_tool(reading, name).route == INLINE, name
        assert route_mcp_tool(writing, name).route == DISPATCH, name


@pytest.mark.parametrize(
    "overrides",
    [
        {"args": ("-m", "caliber.mcp_servers.other")},
        {"args": ("caliber.mcp_servers.db",)},
        {"args": ("--mode", "relational", "-m", "caliber.mcp_servers.db")},
        {"command": "/usr/bin/env"},
        {"command": ""},
        {"transport": "sse", "uri": "https://mcp.example.test/mcp"},
    ],
)
def test_only_a_server_launched_as_the_first_party_module_is_the_first_party_server(
    mcp_world: Any, overrides: dict[str, Any]
) -> None:
    """A server that merely names its tools like the first party's, or launches something else, is third-party."""
    from caliber.mcp_policy import is_first_party_db_server

    server = _first_party(**_policies("insert_rows", level="write"))
    assert is_first_party_db_server(server) is True
    assert is_first_party_db_server(replace(server, **overrides)) is False


def test_the_digest_binds_the_record_and_the_classification_but_no_credential_value() -> None:
    from caliber.effects.mcp_tools import server_digest

    base = _mcp_server()
    policy = {"allowed": True, "side_effect_level": "write", "requires_approval": False}
    digest = server_digest(base, "t", policy)

    for change in (
        {"server_id": "MCP-other"},
        {"transport": "streamable-http"},
        {"uri": "https://mcp.example.test/other"},
        {"command": "x"},
        {"args": ("a",)},
        {"env": {"NEW": "1"}},
        {"headers": {"X": "1"}},
        {"auth_type": "token"},
    ):
        assert server_digest(replace(base, **change), "t", policy) != digest, change
    assert server_digest(base, "other", policy) != digest
    for key in ("allowed", "side_effect_level", "requires_approval"):
        assert server_digest(base, "t", {**policy, key: "x"}) != digest, key
    # The credential's value is not in it: rotating a token keeps the digest, its name changes it.
    with_token = replace(base, env={"API_TOKEN": "a"}, headers={"Authorization": "Bearer a"})
    rotated = replace(base, env={"API_TOKEN": "b"}, headers={"Authorization": "Bearer b"})
    assert server_digest(with_token, "t", policy) == server_digest(rotated, "t", policy)
    assert server_digest(with_token, "t", policy) != digest


@pytest.mark.usefixtures("reference")
def test_mcp_write_tool_requires_proposal(
    factory: sessionmaker[Session], mcp_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write-marked MCP tool never reaches the server without an approved proposal (#759).

    The synchronous gateway entry points refuse it with ``EffectRefused`` and send nothing. Through the runtime the missing
    approval is a refusal before the server is reached; the dispatcher then refuses the very proposal the runtime recorded
    with ``proposal_not_approved``, and the server has still not been called. A ``read`` tool is the one call that runs
    without a proposal.
    """
    from caliber.effects.connectors.mcp_tool import McpToolCall
    from caliber.mcp_gateway import (
        invoke_tool_by_server_id_sync,
        invoke_tool_sync,
        load_server_config,
    )
    from tests.mcp_fakes import SERVER_ID, seed_server, use_database
    from tests.test_mcp_governed_dispatch import McpRun, tool_node_manifest

    use_database(monkeypatch, factory)
    seed_server(factory)

    server = load_server_config(SERVER_ID)
    with pytest.raises(EffectRefused) as refused:
        invoke_tool_sync(server, tool_name="issue_write", arguments={"a": 1})
    assert refused.value.code == "mcp_effect_requires_proposal"
    with pytest.raises(EffectRefused):
        invoke_tool_by_server_id_sync(server_id=SERVER_ID, tool_name="issue_write", arguments={})
    assert mcp_world.calls == []
    assert invoke_tool_sync(server, tool_name="lookup", arguments={}) == {"ok": True}
    mcp_world.calls.clear()

    run = McpRun(factory, tool_node_manifest("issue_write"), authorize=lambda _proposal: None)
    result = run.execute()

    assert result.status == "error"
    assert "authorization was not granted" in (result.error or "")
    (recorded,) = proposals(factory)
    assert recorded.state == "proposed"
    with factory() as session:
        lease = leased(session, "runtime-owner")
        session.commit()
    call = McpToolCall(SERVER_ID, "issue_write", lambda: load_server_config(SERVER_ID))
    with pytest.raises(EffectRefused) as refused:
        dispatch(
            factory,
            recorded.proposal_id,
            approval_id=None,
            lease=lease,
            backend_kind="mcp",
            backend_config={"call": call, "receipt_verifier": verifier},
            approval_validator=_canonical_validator(),
        )
    assert refused.value.code == "proposal_not_approved"
    assert mcp_world.calls == []


#: Every production module that names the MCP gateway's functions, and which it may name. A new caller fails this test until it
#: is routed through the dispatcher or deliberately pinned here with its reason, so an ungoverned alternate call site cannot
#: appear unnoticed. ``invoke_tool_raw_sync`` is the ungated transport: only the dispatcher's backend reaches it.
RAW_MCP_CALLERS: dict[str, tuple[frozenset[str], str]] = {
    "effects/dispatcher.py": (
        frozenset({"mcp_gateway"}),
        "the private mcp backend: the one governed caller of the ungated transport (invoke_tool_raw_sync)",
    ),
    "routes/mcp_servers.py": (
        frozenset({"discover_tools", "invoke_tool"}),
        "the operator's discover, test and playground routes (EP-038 to EP-040, exempt: not agent-reachable)",
    ),
    "workflows/compiler.py": (
        frozenset({"invoke_tool_by_server_id_sync"}),
        "the Python an export generates (EP-043): its gateway entry point runs read-class tools only",
    ),
    "workflows/runtime.py": (
        frozenset({"load_server_config", "invoke_tool_by_server_id_sync"}),
        "inline reads of MCP bindings and nodes (the gateway entry point refuses every other call), "
        "and the server record the governed route classifies from",
    ),
}


#: What a caller can take from the gateway that reaches a server (its errors and configuration type reach nothing).
_GATEWAY_CALLS = frozenset(
    {
        "mcp_gateway",
        "discover_tools",
        "discover_tools_sync",
        "invoke_tool",
        "invoke_tool_sync",
        "invoke_tool_by_server_id_sync",
        "invoke_tool_raw_sync",
        "load_server_config",
    }
)


def _names_taken_from_the_gateway(tree: Any) -> set[str]:

    found: set[str] = set()
    for name in _all_names_taken_from_the_gateway(tree):
        if name in _GATEWAY_CALLS:
            found.add(name)
    return found


def _all_names_taken_from_the_gateway(tree: Any) -> set[str]:
    import ast

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "caliber.mcp_gateway":
                found.update(alias.name for alias in node.names)
            elif node.module == "caliber":
                found.update(alias.name for alias in node.names if alias.name == "mcp_gateway")
        elif isinstance(node, ast.Import):
            found.update(
                alias.name.rsplit(".", 1)[-1]
                for alias in node.names
                if alias.name == "caliber.mcp_gateway"
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            marker = "from caliber.mcp_gateway import "
            if marker in node.value:  # the Python an export generates
                found.add(node.value.split(marker, 1)[1].split()[0].strip(","))
    return found


def test_the_mcp_gateway_is_named_only_by_pinned_callers() -> None:
    import ast
    from pathlib import Path

    import caliber

    root = Path(caliber.__file__).resolve().parent
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if path.name == "mcp_gateway.py":
            continue
        names = _names_taken_from_the_gateway(ast.parse(path.read_text(encoding="utf-8")))
        if names:
            found[path.relative_to(root).as_posix()] = names
    assert found == {file: set(names) for file, (names, _why) in RAW_MCP_CALLERS.items()}


def test_only_the_dispatcher_backend_calls_the_ungated_transport() -> None:
    import ast
    from pathlib import Path

    import caliber

    root = Path(caliber.__file__).resolve().parent
    callers = {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if path.name != "mcp_gateway.py"
        and any(
            isinstance(node, ast.Attribute) and node.attr == "invoke_tool_raw_sync"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert callers == {"effects/dispatcher.py"}


def test_no_classification_is_read_from_what_an_mcp_server_reports() -> None:
    """The policy, the gateway and the route never read a server's hints, annotations or descriptions as a classification."""
    import ast
    from pathlib import Path

    import caliber.effects.connectors.mcp_tool as connector
    import caliber.effects.mcp_tools as route
    import caliber.mcp_gateway as gateway
    import caliber.mcp_policy as policy

    for module in (connector, route, gateway, policy):
        tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value) < 40
        }
        for forbidden in ("readOnlyHint", "destructiveHint", "idempotentHint", "annotations"):
            assert forbidden not in names | strings, (module.__name__, forbidden)


def test_the_development_profile_classifies_the_same_but_refuses_nothing_by_inventory(
    mcp_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's decision is profile-independent; EP-114's refusal and the gateway's refusal are the reference profile's."""
    import sys

    from caliber.effects.mcp_tools import route_mcp_tool

    monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", sys.executable)
    server = _first_party(**_policies("insert_rows", level="write"))

    monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    development = route_mcp_tool(server, "insert_rows")
    monkeypatch.setenv("CALIBER_PROFILE", "reference")
    reference = route_mcp_tool(server, "insert_rows")

    assert (development.route, development.legacy_inline_allowed, development.first_party) == (
        DISPATCH,
        True,
        True,
    )
    assert (reference.route, reference.legacy_inline_allowed, reference.code) == (
        REFUSE,
        False,
        "unsupported_effect_path",
    )


def _sidecar(host: str = "mcp-db-relational", **overrides: Any) -> Any:
    return _mcp_server(
        transport="streamable-http",
        uri=f"http://{host}:8101/mcp",
        **_policies("insert_rows", level="write"),
        **overrides,
    )


def test_a_server_on_an_attested_managed_sidecar_host_is_treated_as_the_first_party_one(
    mcp_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped compose stack runs the first-party DB servers as streamable-http sidecars on attested hosts."""
    from caliber.effects.mcp_tools import INVENTORY_REFUSAL, route_mcp_tool
    from caliber.mcp_policy import is_first_party_db_server

    monkeypatch.setenv("CALIBER_MCP_MANAGED_SIDECAR_HOSTS", "mcp-db-relational,mcp-db-vector")
    monkeypatch.setenv("CALIBER_MCP_REMOTE_HOST_ALLOWLIST", "mcp-db-relational,mcp-db-vector,other")
    monkeypatch.setenv("CALIBER_MCP_ALLOW_INSECURE_HTTP", "true")
    monkeypatch.setenv("CALIBER_PROFILE", "reference")

    assert is_first_party_db_server(_sidecar("mcp-db-relational")) is True
    assert is_first_party_db_server(_sidecar("MCP-DB-VECTOR")) is True
    assert is_first_party_db_server(replace(_sidecar("mcp-db-relational"), transport="sse")) is True
    assert is_first_party_db_server(_sidecar("other")) is False
    assert is_first_party_db_server(replace(_sidecar(), uri="not a url")) is False
    assert is_first_party_db_server(replace(_sidecar(), transport="carrier-pigeon")) is False
    decided = route_mcp_tool(_sidecar("mcp-db-relational"), "insert_rows")
    assert (decided.route, decided.code) == (REFUSE, INVENTORY_REFUSAL)
    assert route_mcp_tool(_sidecar("other"), "insert_rows").route == DISPATCH


def test_a_profile_that_is_not_development_is_fail_closed_whatever_its_name(
    mcp_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy side is an allow-list (``== "development"``): a profile added later refuses like the reference one."""
    import sys

    from caliber.config import CaliberConfig
    from caliber.effects.dispatcher import UnsupportedEffectPath
    from caliber.effects.mcp_tools import route_mcp_tool
    from caliber.mcp_gateway import _require_inline

    monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", sys.executable)
    base = CaliberConfig.load()
    # The setting accepts only two names today, so a third is built without validation, as a future one would be.
    staging = CaliberConfig.model_construct(**{**base.model_dump(), "profile": "staging"})
    monkeypatch.setattr(CaliberConfig, "load", classmethod(lambda cls, environ=None: staging))
    plain = _mcp_server()
    first_party = _first_party(**_policies("insert_rows", level="write"))

    assert route_mcp_tool(plain, "t", config=staging).legacy_inline_allowed is False
    with pytest.raises(EffectRefused):
        _require_inline(plain, "t")
    with pytest.raises(UnsupportedEffectPath):
        _require_inline(first_party, "insert_rows")


def test_the_development_profile_legacy_entry_is_named_only_by_the_runtime() -> None:
    assert _modules_naming({"bind_legacy_openapi_http_tool"}) == set(LEGACY_OPENAPI_CALLERS)


def test_the_development_profile_preview_entry_is_named_only_by_the_preview_route() -> None:
    assert _modules_naming({"execute_legacy_openapi_draft_preview"}) == set(LEGACY_PREVIEW_CALLERS)


def test_the_private_raw_transport_is_named_only_by_pinned_modules() -> None:
    """A write reaches the network only through ``_send_raw``: a module that starts naming it fails here."""
    assert _modules_naming({"_send_raw"}) == set(RAW_TRANSPORT_CALLERS)


@pytest.mark.parametrize(
    "source",
    [
        "from caliber.integrations.openapi.executor import _send_raw\n",
        "from caliber.integrations.openapi import executor\nexecutor._send_raw()\n",
        "x = _send_raw\n",
        "def _send_raw():\n    pass\n",
        "from caliber.integrations.openapi.executor import _send_raw as send\n",
        'import importlib\nimportlib.import_module("x").__dict__["_send_raw"]\n',
        'getattr(executor, "_send_raw")(**kwargs)\n',
    ],
)
def test_the_raw_transport_scan_sees_every_spelling_of_the_name(source: str) -> None:
    import ast

    assert any(_names_the_raw_executor(node, {"_send_raw"}) for node in ast.walk(ast.parse(source)))


def test_the_raw_transport_scan_ignores_the_public_entry() -> None:
    import ast

    source = "from caliber.integrations.openapi.executor import execute_openapi_http_tool\n"
    assert not any(
        _names_the_raw_executor(node, {"_send_raw"}) for node in ast.walk(ast.parse(source))
    )


def test_the_raw_transport_scan_ignores_prose_that_only_mentions_the_name() -> None:
    import ast

    source = '"""Sends through ``executor._send_raw`` (the private transport)."""\nx = "other"\n'
    assert not any(
        _names_the_raw_executor(node, {"_send_raw"}) for node in ast.walk(ast.parse(source))
    )


# ------------------------------------------------------ B04.6 (#768): model-selected calls


def test_model_tool_call_returns_proposal_not_effect(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oracle of #768: a model's call of a write tool leaves a ``proposed`` row and the downstream counter at zero.

    The call is the model's (a governed step's scripted adapter proposes it); the tool is a registered write tool whose row,
    not the model, classifies it. The full matrix (every kind, every refusal) is ``tests/test_model_selected_proposals.py``
    and ``tests/test_model_selected_calls*.py``; the call sites are pinned by ``tests/test_model_tool_call_sites.py``.
    """
    from tests.mcp_fakes import use_database
    from tests.test_model_selected_calls import SENTINEL
    from tests.test_model_selected_proposals import (
        _registered,
        _registered_action,
        parked,
        propose,
    )

    use_database(monkeypatch, factory)
    world = _registered(factory)
    downstream = Tool()
    result = propose(world, _registered_action(args={"title": SENTINEL}), open_ticket=downstream)

    proposal, _ = parked(world, result)
    assert proposal.state == "proposed"
    assert downstream.calls == [], "downstream.mutations() == 0"
