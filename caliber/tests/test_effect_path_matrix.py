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
    "routes/system_effects.py": "the operator's lookup by key: a GET that carries no effect",
    "workflows/runtime.py": "inline reads of OpenAPI tool nodes; model-chosen calls (B04.6, #768) and exports (#1051) can read but not write",
}

#: Every production module that names the PRIVATE raw transport (``_send_raw``), which sends any method with no governance
#: of its own, and why it may. It is the only door through which a write reaches the network, so the set is deliberately
#: tiny and a new name here is a design decision.
RAW_TRANSPORT_CALLERS = {
    "effects/dispatcher.py": "the private connector backends (document request, OpenAPI): the dispatcher's own transport",
    "integrations/openapi/executor.py": "defines it; the public entry delegates to it only after refusing a protected method",
    "routes/openapi_integrations.py": "the operator's draft preview route (EP-033, exempt: one real call fired by a human, gated by allow_in_preview)",
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
