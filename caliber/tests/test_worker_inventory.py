"""Tests for the machine-readable background-worker inventory (`P0-A`).

Mirrors `test_route_scope_inventory.py`'s two-tier shape: derivation unit
tests against the real `server.py`, plus a completeness/staleness gate
between what's derived and the hand-maintained `_WORKER_NOTES` registry.
"""

from __future__ import annotations

from caliber.db import models
from caliber.observability.worker_inventory import (
    _WORKER_NOTES,
    discover_lifespan_workers,
    worker_inventory,
)

#: The 9 workers known to be started by server.py::_build_lifespan today,
#: in startup order. A literal pin: the whole point of deriving rather than
#: declaring is that a worker silently added to or removed from the
#: lifespan changes this list without anyone updating a hand-maintained
#: count -- so this test must notice.
_EXPECTED_WORKERS = [
    ("worker", "RefinementWorker"),
    ("janitor", "JanitorTask"),
    ("release_reconciler", "ReleaseReconcilerTask"),
    ("calibration_drain_task", "CalibrationDrain"),
    ("webhooks", "WebhookDispatcher"),
    ("workflow_run_worker", "WorkflowRunWorker"),
    ("aria_plan_worker", "AriaPlanWorker"),
    ("knowledge_build_worker", "KnowledgeBaseWorker"),
    ("scheduler", "WorkflowSchedulerTask"),
]


def test_discovers_every_known_lifespan_worker() -> None:
    discovered = discover_lifespan_workers()
    assert set(discovered) == set(_EXPECTED_WORKERS), (
        f"discover_lifespan_workers() found {sorted(discovered)}, expected "
        f"{sorted(_EXPECTED_WORKERS)} -- if a worker was added to or removed "
        f"from server.py::_build_lifespan, update this pin (and "
        f"_WORKER_NOTES if it's an addition)."
    )
    assert len(discovered) == len(_EXPECTED_WORKERS), "no duplicate names"


def test_every_discovered_worker_has_a_registry_note() -> None:
    """A worker added to the lifespan with no _WORKER_NOTES entry fails by
    name -- the same completeness discipline
    test_route_scope_inventory.py enforces for scope classifications."""
    discovered_classes = {cls_name for _name, cls_name in discover_lifespan_workers()}
    missing = sorted(discovered_classes - set(_WORKER_NOTES))
    assert not missing, (
        f"these workers have no _WORKER_NOTES entry in "
        f"caliber/observability/worker_inventory.py: {missing}"
    )


def test_no_stale_registry_notes() -> None:
    """A note for a worker no longer started by the lifespan is a stale
    claim -- mirrors test_route_scope_inventory.py's staleness check."""
    discovered_classes = {cls_name for _name, cls_name in discover_lifespan_workers()}
    stale = sorted(set(_WORKER_NOTES) - discovered_classes)
    assert not stale, f"_WORKER_NOTES names workers no longer in the lifespan: {stale}"


def test_every_declared_table_name_exists_on_models() -> None:
    """Catches a typo or a model rename the registry wasn't updated for --
    not a claim that any worker's table list is complete."""
    bad: list[tuple[str, str]] = []
    for descriptor in worker_inventory():
        for table in descriptor.tables:
            if not hasattr(models, table):
                bad.append((descriptor.cls_name, table))
    assert not bad, f"table names with no matching caliber.db.models class: {bad}"


def test_every_worker_has_a_description() -> None:
    for descriptor in worker_inventory():
        assert descriptor.description, f"{descriptor.cls_name} has no description"


def test_only_workflow_run_worker_registers_a_heartbeat() -> None:
    """Regression pin: caliber.observability.worker_registry.record_heartbeat
    is called from exactly one place in the codebase today
    (WorkflowRunWorker._record_liveness). Derived, not hand-declared -- if a
    second worker starts reporting heartbeats, this test documents that the
    inventory noticed rather than silently continuing to say "no".
    """
    by_class = {d.cls_name: d.registers_heartbeat for d in worker_inventory()}
    assert by_class["WorkflowRunWorker"] is True
    heartbeating = sorted(cls for cls, registers in by_class.items() if registers)
    assert heartbeating == ["WorkflowRunWorker"]


def test_worker_inventory_covers_every_expected_class() -> None:
    cls_names = {d.cls_name for d in worker_inventory()}
    assert cls_names == {cls_name for _name, cls_name in _EXPECTED_WORKERS}
