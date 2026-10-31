"""Which way a registry tool runs: inline, through the dispatcher, or not at all (B04.3, #741; ADR 0002).

A workflow tool node names a registered tool. Whether that call changes the outside world is decided here, from the
tool's **registry row** (name, owner, version, module and callable, read from the database for the tool being run), and
never from the manifest binding, the model, or an exported bundle: those are written by whoever authored the workflow.

* A row that is CALIBER's own built-in (``workflows.builtin_tools.is_builtin_tool``) takes the protected-effect
  inventory's class, or the class its declared side-effect level implies when that is stricter.
* A row that reuses a built-in's reserved name without being the seed's own row, or a tool whose row cannot be read, is
  classified :data:`~caliber.effects.inventory.FAIL_CLOSED`.
* Any other row is an administrator-registered tool. The inventory does not list it, so the row's own
  ``side_effect_level`` is the registry's classification of record: ``read`` runs inline, ``write`` is a
  ``reversible_write`` and ``external_action`` an ``irreversible_write``, and an unrecognised level is fail-closed.
  That level is pinned by the deployment bundle (``deployment_bundle.tool_snapshot``) and re-verified before a run, so
  it is the registry's word and not the binding's.
* A built-in the reference profile refuses (inventory disposition ``refuse``: EP-055) is refused.

A registered **OpenAPI** tool (``execution_backend: openapi_http``, B04.4) is classified by :func:`route_openapi_tool`: the same
row-level rules, plus the method of the operation the row's own configuration selects, which can only make it stricter (a
``POST`` of a row declared ``read`` is not read-class). The row's ``backend_config`` and ``input_schema`` are part of its
identity, so the runtime can refuse a binding that carries anything else.

Only :func:`route_registered_tool` (and, for OpenAPI rows, :func:`route_openapi_tool`) decides; the dispatcher still resolves the proposal's recorded class itself
(``submit_proposal``), so an administrator-registered write tool is recorded as ``irreversible_write``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberToolRegistry
from caliber.effects.connectors.openapi_http import READ_METHODS
from caliber.effects.inventory import (
    EFFECT_CLASSES,
    FAIL_CLOSED,
    effect_class_for_registry_tool,
    tool_disposition_for_registry_tool,
)
from caliber.workflows.builtin_tools import is_builtin_tool, reserved_tool_name

REGISTERED_TOOL_CONNECTOR: Final = "registered_tool"
REGISTERED_TOOL_VERSION: Final = "1"

#: The ``execution_backend`` of a registry row that is an OpenAPI operation or tool pack (B04.4).
OPENAPI_BACKEND: Final = "openapi_http"

INLINE: Final = "inline"
DISPATCH: Final = "dispatch"
REFUSE: Final = "refuse"

#: The effect class a registry row's declared ``side_effect_level`` implies (the same floor
#: ``tests/test_protected_effect_inventory.py`` holds the built-ins to).
_DECLARED_CLASS: Final = {
    "read": "read",
    "write": "reversible_write",
    "external_action": "irreversible_write",
}


@dataclass(frozen=True)
class RegisteredToolIdentity:
    """A registry row's own identity fields: what the platform trusts, as opposed to what a binding says."""

    name: str
    owner: str
    version: str
    module_path: str
    callable_name: str
    side_effect_level: str
    execution_backend: str = "python_callable"
    #: The row's own ``backend_config`` and ``input_schema``: what an OpenAPI tool sends (B04.4). The manifest binding carries
    #: copies; the row's are the ones a governed call uses, and a binding that differs from them is refused.
    backend_config: Mapping[str, Any] | None = field(default=None, repr=False)
    input_schema: Mapping[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolRoute:
    """The decision for one tool call: ``route`` is :data:`INLINE`, :data:`DISPATCH` or :data:`REFUSE`."""

    route: str
    effect_class: str
    trusted: bool


def load_registered_tool_identity(
    session: Session, *, name: str, version: str
) -> RegisteredToolIdentity | None:
    """The registry row for ``(name, version)``, or ``None`` when there is none (the pair is unique)."""
    row = session.execute(
        select(CaliberToolRegistry).where(
            CaliberToolRegistry.name == name, CaliberToolRegistry.version == version
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return RegisteredToolIdentity(
        name=row.name,
        owner=row.owner,
        version=row.version,
        module_path=row.module_path,
        callable_name=row.callable_name,
        side_effect_level=row.side_effect_level,
        execution_backend=row.execution_backend,
        backend_config=row.backend_config,
        input_schema=row.input_schema,
    )


def registry_identity_reader(
    session_factory: Callable[[], Any],
) -> Callable[[str, str], RegisteredToolIdentity | None]:
    """A reader of a tool's own registry row, one fresh session per read: what ``RuntimePlan.registered_tool_identity`` takes."""

    def read(name: str, version: str) -> RegisteredToolIdentity | None:
        with session_factory() as session:
            return load_registered_tool_identity(session, name=name, version=version)

    return read


def route_registered_tool(identity: RegisteredToolIdentity | None) -> ToolRoute:
    """Decide how a tool runs from its registry identity; an unverifiable tool is dispatched fail-closed."""
    if identity is None:
        return ToolRoute(DISPATCH, FAIL_CLOSED, trusted=False)
    trusted = is_builtin_tool(
        name=identity.name,
        owner=identity.owner,
        module_path=identity.module_path,
        callable_name=identity.callable_name,
        version=identity.version,
    )
    inventory_class = effect_class_for_registry_tool(
        name=identity.name,
        owner=identity.owner,
        module_path=identity.module_path,
        callable_name=identity.callable_name,
        version=identity.version,
    )
    disposition = tool_disposition_for_registry_tool(
        name=identity.name,
        owner=identity.owner,
        module_path=identity.module_path,
        callable_name=identity.callable_name,
        version=identity.version,
    )
    if disposition == REFUSE:
        return ToolRoute(REFUSE, inventory_class, trusted)
    declared = _DECLARED_CLASS.get(identity.side_effect_level, FAIL_CLOSED)
    if trusted:
        effect = max(inventory_class, declared, key=EFFECT_CLASSES.index)
    elif reserved_tool_name(identity.name) is not None:
        effect = FAIL_CLOSED
    else:
        effect = declared
    return ToolRoute(INLINE if effect == "read" else DISPATCH, effect, trusted)


def route_openapi_tool(identity: RegisteredToolIdentity | None, *, method: str) -> ToolRoute:
    """Decide how a registered OpenAPI tool's call runs, from its registry row and the method its configuration resolves to (B04.4, #750).

    The class is the stricter of two things the registry holds: the row's declared ``side_effect_level`` (as for any
    registered tool) and the **method** of the operation the row's own configuration selects (a pack's operation is chosen
    by the input from the row's list, never from the binding): a method that is not ``GET``, ``HEAD`` or ``OPTIONS`` is at
    least a ``reversible_write`` whatever the level says, so a row that under-declares cannot make a ``POST`` inline. Only a
    ``read``-class result runs inline; everything else is dispatched. A tool with no readable row, or a row that is not an
    OpenAPI tool, cannot be proposed (there is no trusted operation to send) and is refused.
    """
    if identity is None or identity.execution_backend != OPENAPI_BACKEND:
        return ToolRoute(REFUSE, FAIL_CLOSED, trusted=False)
    base = route_registered_tool(identity)
    if base.route == REFUSE:
        return base
    floor = "read" if method.upper() in READ_METHODS else "reversible_write"
    effect = max(base.effect_class, floor, key=EFFECT_CLASSES.index)
    return ToolRoute(INLINE if effect == "read" else DISPATCH, effect, trusted=False)


def tool_resource(name: str, version: str) -> str:
    """The proposal's ``resource`` for a registry tool: the target a reviewer approves."""
    return f"registered-tool/{name}/{version}"


def tool_intent_args(
    *, name: str, version: str, arguments: dict[str, Any], input_text: str
) -> dict[str, Any]:
    """The immutable arguments of a registry-tool proposal, which the backend replays and nothing else."""
    return {"tool": name, "version": version, "arguments": arguments, "input": input_text}


__all__ = [
    "DISPATCH",
    "INLINE",
    "OPENAPI_BACKEND",
    "REFUSE",
    "REGISTERED_TOOL_CONNECTOR",
    "REGISTERED_TOOL_VERSION",
    "RegisteredToolIdentity",
    "ToolRoute",
    "load_registered_tool_identity",
    "registry_identity_reader",
    "route_openapi_tool",
    "route_registered_tool",
    "tool_intent_args",
    "tool_resource",
]
