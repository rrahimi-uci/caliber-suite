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
    route_registered_tool,
    tool_intent_args,
    tool_resource,
)
from caliber.workflows.builtin_tools import BUILTIN_TOOL_NAMES, register_builtin_tools
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
