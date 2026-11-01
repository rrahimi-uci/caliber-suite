"""The reference profile's refusal switch (B04.7, #774; ADR 0001).

A path the protected-effect inventory marks ``refuse`` runs arbitrary source, imports an arbitrary module, exports
runnable code or unconfined host data, or writes to a database or a host folder with no dispatcher in between. In the
reference profile (``config.profile == "reference"``) it fails with :class:`~caliber.effects.dispatcher.UnsupportedEffectPath`
and the stable code :data:`UNSUPPORTED_IN_REFERENCE_PROFILE`; the message names the inventory row and entry point and the
setting that allows the path elsewhere. Outside the reference profile nothing changes: a refusal is a reference-profile
control, never a development-profile one (ADR 0001, "Unsupported paths are refused, not degraded").

Every refusing call site names its inventory row (``refuse_in_reference_profile("EP-011", config)``). The row must exist and
must be a ``refuse`` row, so a typo, or a row someone later re-classifies, fails loudly instead of refusing nothing;
``tests/test_effect_path_matrix.py`` checks that every ``refuse`` row has a site that raises and that every site names a
``refuse`` row. The caller passes **the configuration it has** (a plan's operator configuration, a request's app config, the
environment of a separate process); no configuration means no profile was named, which is the development default, so a
helper that is handed none never refuses by accident.

CALIBER's own shipped read-class tools (the seeded built-ins) are the one exemption, and only for the tool-execution rows
(:func:`refuse_unshipped_tool_in_reference_profile`): they are code CALIBER ships and tests, identified by the **pair** of
module and callable the registry seed writes (``BUILTIN_TOOL_MODULE_BY_NAME``), never by a name, an owner or what a registry
row or a manifest says. A built-in the inventory itself refuses (EP-054, EP-055) is not exempt.

The module is light on purpose: the sandbox, the database MCP server and the route modules import it, and the exception
class lives in the dispatcher, which is imported only when a refusal is actually raised.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

from caliber.effects.inventory import InventoryRow, inventory_refuses_tool_name, load_inventory

if TYPE_CHECKING:
    from caliber.effects.dispatcher import UnsupportedInReferenceProfile

#: The stable code of every refusal this module raises (ADR 0001).
UNSUPPORTED_IN_REFERENCE_PROFILE: Final = "unsupported_in_reference_profile"
#: What the message tells an operator to set to use the path outside the reference profile.
ALLOWING_SETTING: Final = "CALIBER_PROFILE=development"


def reference_profile_active(config: object | None) -> bool:
    """Whether ``config`` says the reference profile. ``None`` (no configuration) is not the reference profile."""
    return getattr(config, "profile", "") == "reference"


def profile_from_environment() -> Any:
    """A minimal configuration naming the profile a separate process was started under (``CALIBER_PROFILE``).

    For code that runs in its own process (the standalone sandbox service, the first-party database MCP server) and has no
    application configuration object. An unset or unknown value is the development default.
    """
    return SimpleNamespace(profile=os.environ.get("CALIBER_PROFILE", "").strip())


def refused_row(row_id: str) -> InventoryRow:
    """The inventory row ``row_id``, which must be a ``refuse`` row."""
    for row in load_inventory():
        if row.id == row_id:
            if row.disposition != "refuse":
                raise ValueError(f"{row_id} is {row.disposition!r} in the inventory, not 'refuse'")
            return row
    raise ValueError(f"{row_id} is not a row of the protected-effect inventory")


def refusal(row_id: str) -> UnsupportedInReferenceProfile:
    """The refusal for inventory row ``row_id`` (an ``UnsupportedEffectPath``): a stable code, and a message with no payload."""
    from caliber.effects.dispatcher import UnsupportedInReferenceProfile  # noqa: PLC0415 - import cycle

    row = refused_row(row_id)
    return UnsupportedInReferenceProfile(
        UNSUPPORTED_IN_REFERENCE_PROFILE,
        f"{UNSUPPORTED_IN_REFERENCE_PROFILE}: {row.id} ({row.entry_point}) is not supported in the reference profile; "
        f"set {ALLOWING_SETTING} to use it outside the reference profile",
    )


def refuse_in_reference_profile(row_id: str, config: object | None) -> None:
    """Raise the refusal for ``row_id`` when ``config`` is the reference profile; otherwise do nothing."""
    row = refused_row(row_id)  # a wrong row id fails in every profile, not only the reference one
    if reference_profile_active(config):
        raise refusal(row.id)


def shipped_tool(module_path: str, callable_name: str) -> bool:
    """Whether (``module_path``, ``callable_name``) is one of CALIBER's own seeded built-ins that the inventory does not refuse.

    The registry seed writes each built-in's own module (``BUILTIN_TOOL_MODULE_BY_NAME``), so the pair, unlike a name, an owner
    or a registry row, is something an administrator cannot reproduce with a different module. A built-in whose inventory
    disposition is ``refuse`` (the host-file tools, ``sandbox_python``) is not allowed.
    """
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE_BY_NAME  # noqa: PLC0415

    return BUILTIN_TOOL_MODULE_BY_NAME.get(
        callable_name
    ) == module_path and not inventory_refuses_tool_name(callable_name)


def refuse_unshipped_tool_in_reference_profile(
    row_id: str, config: object | None, *, module_path: str, callable_name: str
) -> None:
    """:func:`refuse_in_reference_profile` unless the tool is one CALIBER ships (:func:`shipped_tool`)."""
    row = refused_row(row_id)
    if reference_profile_active(config) and not shipped_tool(module_path, callable_name):
        raise refusal(row.id)


__all__ = [
    "ALLOWING_SETTING",
    "UNSUPPORTED_IN_REFERENCE_PROFILE",
    "profile_from_environment",
    "reference_profile_active",
    "refusal",
    "refuse_in_reference_profile",
    "refuse_unshipped_tool_in_reference_profile",
    "refused_row",
    "shipped_tool",
]
