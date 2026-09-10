"""Tests for the machine-readable per-model project-scoping inventory
(`P0-A`).

Unlike `test_route_scope_inventory.py`/`test_worker_inventory.py`, nothing
here is hand-maintained -- every fact is safely, mechanically derivable, so
these tests check derivation correctness and one genuinely load-bearing
safety invariant, not completeness against a registry.
"""

from __future__ import annotations

from collections import Counter

from caliber.db.resource_inventory import (
    SCOPING_OWNED_CATALOG,
    SCOPING_PROJECT_ONLY,
    SCOPING_UNSCOPED,
    SCOPING_VISIBILITY,
    resource_inventory,
)
from caliber.db.scoping import owner_column

#: Today's exact per-category split, confirmed by direct introspection
#: against the live model registry. A ratchet, not a law: a new model
#: changing these numbers is expected over time. The point of pinning them
#: is that the change gets *noticed and looked at* (was this model's
#: scoping tier a deliberate choice?), the same spirit as
#: test_async_offload_ratchet.py's baseline.
_EXPECTED_COUNTS = {
    SCOPING_UNSCOPED: 40,
    SCOPING_OWNED_CATALOG: 24,
    SCOPING_VISIBILITY: 14,
    SCOPING_PROJECT_ONLY: 7,
}


def test_every_known_model_is_classified_exactly_once() -> None:
    from caliber.db.base import Base

    rows = resource_inventory()
    names = [r.name for r in rows]
    assert len(names) == len(set(names)), "no model should appear twice"
    assert len(rows) == len(list(Base.registry.mappers))


def test_distribution_matches_todays_ratchet() -> None:
    rows = resource_inventory()
    counts = Counter(r.scoping for r in rows)
    assert dict(counts) == _EXPECTED_COUNTS, (
        f"scoping distribution changed: {dict(counts)} vs expected "
        f"{_EXPECTED_COUNTS}. If this is a deliberate new model, update "
        f"_EXPECTED_COUNTS here after confirming its scoping tier is "
        f"correct -- this is a ratchet meant to force that look, not a "
        f"number that should never move."
    )


def test_no_visibility_model_is_missing_project_id_or_an_owner_column() -> None:
    """The one genuinely load-bearing check: apply_visibility_filter's own
    docstring requires visibility + project_id + an owner column together.
    A model with visibility but missing either would raise the first time
    a non-admin hit it -- exactly the historical CaliberEvalRun defect
    db.scoping.owner_column's docstring names. Zero such cases exist today;
    this keeps it that way."""
    broken = [
        r.name
        for r in resource_inventory()
        if r.scoping == SCOPING_VISIBILITY and (not r.has_project_id or not r.owner_column)
    ]
    assert not broken, (
        f"these models have 'visibility' but are missing project_id or an "
        f"owner column, which would break apply_visibility_filter's "
        f"duck-typed contract: {broken}"
    )


def test_visibility_models_owner_column_call_succeeds_for_real() -> None:
    """Call-through, not re-derivation: proves the inventory's claim
    against the actual function routes depend on, not a second guess at
    what it would do."""
    from caliber.db import models

    for row in resource_inventory():
        if row.scoping != SCOPING_VISIBILITY:
            continue
        model = getattr(models, row.name)
        column = owner_column(model)  # must not raise
        assert column.key == row.owner_column


def test_project_only_models_have_no_visibility_column() -> None:
    for row in resource_inventory():
        if row.scoping == SCOPING_PROJECT_ONLY:
            assert row.has_project_id
            assert not row.has_visibility


def test_owned_catalog_models_have_no_project_id() -> None:
    for row in resource_inventory():
        if row.scoping == SCOPING_OWNED_CATALOG:
            assert row.owner_column is not None
            assert not row.has_project_id


def test_unscoped_models_have_neither_project_id_nor_owner_column() -> None:
    for row in resource_inventory():
        if row.scoping == SCOPING_UNSCOPED:
            assert not row.has_project_id
            assert row.owner_column is None
