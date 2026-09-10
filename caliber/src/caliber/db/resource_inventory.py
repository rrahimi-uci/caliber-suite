"""Machine-readable per-model project-scoping inventory.

Part of `P0-A` (`docs/workspace-plan.md` section 16, Phase 0 item 2/3): after
the route/scope inventory (`routes/scope_inference.py`) and the worker
inventory (`observability/worker_inventory.py`), this is the resource-root
slice -- "resource root ... project lookup, owner column" / "every table's
project FK ... [and] visibility."

Unlike the other two inventories, nothing here needs a hand-maintained
note. Every fact is safely, mechanically derivable, because
:mod:`caliber.db.scoping` already contains the single source of truth for
how a model's ownership column is resolved
(:func:`caliber.db.scoping.owner_column`, which tries ``owner`` then
``created_by`` and raises ``TypeError`` for neither) -- this reuses that
function directly rather than re-deriving the same fact a second way, the
same "don't hand-duplicate a derivable truth" discipline the other two
inventories apply to `require_scopes()` calls and `_build_lifespan`'s
`await <name>.start()` calls respectively.

Classifying every :class:`caliber.db.models.Base` subclass this way finds a
clean 4-way partition with no ambiguous cases: ``visibility`` (the full
3-tier model -- ``project_id`` + ``visibility`` + a resolvable owner
column), ``project_only`` (``project_id`` but no ``visibility`` column --
scoped via ``require_project_access`` directly against the project, not the
3-tier scheme), ``owned_catalog`` (a resolvable owner column but no
``project_id`` -- personal/public catalog resources), and ``unscoped``
(neither -- pure operational/system state). The one genuinely load-bearing
check this inventory enables: a model with ``visibility`` but missing
``project_id`` or an owner column would raise the first time a non-admin
hit it -- exactly the historical `CaliberEvalRun` defect
:func:`caliber.db.scoping.owner_column`'s own docstring names.
`tests/test_resource_inventory.py` asserts zero such cases today and keeps
asserting it going forward.
"""

from __future__ import annotations

from dataclasses import dataclass

# Importing anything from the caliber.db package runs caliber/db/__init__.py,
# which imports caliber.db.models fully -- registering every Caliber*(Base)
# class on Base.registry as a side effect. resource_inventory() below relies
# on this (rather than importing caliber.db.models directly, which would
# only prove the classes it re-exports by name are registered, not the ones
# it doesn't) -- the same reason `from caliber.db.base import Base` alone is
# enough for Base.registry.mappers to be complete.
from caliber.db.base import Base
from caliber.db.scoping import owner_column

SCOPING_VISIBILITY = "visibility"
SCOPING_PROJECT_ONLY = "project_only"
SCOPING_OWNED_CATALOG = "owned_catalog"
SCOPING_UNSCOPED = "unscoped"


@dataclass(frozen=True)
class ResourceDescriptor:
    """One `Base` subclass, classified by its project/ownership/visibility
    columns."""

    name: str  # model class name, e.g. "CaliberEvalRun"
    table: str  # __tablename__
    has_project_id: bool
    owner_column: str | None  # "owner" | "created_by" | None
    has_visibility: bool
    scoping: str  # SCOPING_* above


def _resolved_owner_column_name(model: type) -> str | None:
    try:
        return str(owner_column(model).key)
    except TypeError:
        return None


def _classify(*, has_project_id: bool, has_visibility: bool, owner: str | None) -> str:
    if has_visibility:
        return SCOPING_VISIBILITY
    if has_project_id:
        return SCOPING_PROJECT_ONLY
    if owner is not None:
        return SCOPING_OWNED_CATALOG
    return SCOPING_UNSCOPED


def resource_inventory() -> list[ResourceDescriptor]:
    """Every `caliber.db.models.Caliber*(Base)` class, classified by its
    project/ownership/visibility columns.

    Enumerated via SQLAlchemy's own live mapper registry
    (`Base.registry.mappers`) rather than by importing
    `caliber.db.models` and filtering its module namespace, which would
    miss any model class that isn't a module-level public name.
    """
    descriptors: list[ResourceDescriptor] = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        columns = set(mapper.columns.keys())
        has_project_id = "project_id" in columns
        has_visibility = "visibility" in columns
        owner = _resolved_owner_column_name(model)
        descriptors.append(
            ResourceDescriptor(
                name=model.__name__,
                table=str(getattr(model, "__tablename__", "")),
                has_project_id=has_project_id,
                owner_column=owner,
                has_visibility=has_visibility,
                scoping=_classify(
                    has_project_id=has_project_id, has_visibility=has_visibility, owner=owner
                ),
            )
        )
    return descriptors


__all__ = [
    "SCOPING_OWNED_CATALOG",
    "SCOPING_PROJECT_ONLY",
    "SCOPING_UNSCOPED",
    "SCOPING_VISIBILITY",
    "ResourceDescriptor",
    "resource_inventory",
]
