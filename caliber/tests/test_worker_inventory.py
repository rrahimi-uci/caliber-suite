"""Tests for the machine-readable background-worker inventory (`P0-A`).

Mirrors `test_route_scope_inventory.py`'s two-tier shape: derivation unit
tests against the real `server.py`, plus a completeness/staleness gate
between what's derived and the hand-maintained `_WORKER_NOTES` registry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from caliber.db import models
from caliber.observability.worker_inventory import (
    _WORKER_NOTES,
    OWNERSHIP_VALUES,
    WorkerNote,
    discover_lifespan_workers,
    worker_inventory,
)

ADR_PATH = Path(__file__).resolve().parents[2] / "docs/architecture/adr/0002-execution-ownership.md"

#: The 12 workers known to be started by server.py::_build_lifespan today,
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
    ("outbox_publisher", "OutboxPublisher"),
    ("workflow_run_worker", "WorkflowRunWorker"),
    ("aria_plan_worker", "AriaPlanWorker"),
    ("knowledge_build_worker", "KnowledgeBaseWorker"),
    ("workspace_import_worker", "WorkspaceImportWorker"),
    ("scheduler", "WorkflowSchedulerTask"),
    ("workspace_release_evaluator", "WorkspaceReleaseEvaluator"),
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


def test_only_the_workers_that_report_liveness_register_a_heartbeat() -> None:
    """Regression pin: caliber.observability.worker_registry.record_heartbeat
    is called from exactly two places in the codebase today
    (WorkflowRunWorker._record_liveness and WorkspaceReleaseEvaluator._record_liveness,
    B10.1). Derived, not hand-declared -- if another worker starts reporting
    heartbeats, this test documents that the inventory noticed rather than
    silently continuing to say "no".
    """
    by_class = {d.cls_name: d.registers_heartbeat for d in worker_inventory()}
    assert by_class["WorkflowRunWorker"] is True
    assert by_class["WorkspaceReleaseEvaluator"] is True
    heartbeating = sorted(cls for cls, registers in by_class.items() if registers)
    assert heartbeating == ["WorkflowRunWorker", "WorkspaceReleaseEvaluator"]


def test_workspace_release_evaluator_is_listed_owner_only_with_its_tables() -> None:
    """B10.1 (#701): the evaluator is discovered from the lifespan and classified for the lease."""
    (descriptor,) = [d for d in worker_inventory() if d.cls_name == "WorkspaceReleaseEvaluator"]
    assert descriptor.name == "workspace_release_evaluator"
    assert descriptor.ownership == "owner_only"
    assert descriptor.registers_heartbeat is True
    assert descriptor.tables == ("CaliberWorkspaceReleaseEvaluation", "CaliberWorkerHeartbeat")


def test_worker_inventory_covers_every_expected_class() -> None:
    cls_names = {d.cls_name for d in worker_inventory()}
    assert cls_names == {cls_name for _name, cls_name in _EXPECTED_WORKERS}


def test_ownership_values_are_the_two_documented_modes() -> None:
    assert OWNERSHIP_VALUES == ("owner_only", "any_process")


def test_every_loop_has_ownership() -> None:
    """Every discovered lifespan worker declares who may run it; a loop with no
    classification, or one outside the two modes, fails by name."""
    bad = {
        d.cls_name: d.ownership for d in worker_inventory() if d.ownership not in OWNERSHIP_VALUES
    }
    assert not bad, f"workers with no valid ownership in worker_inventory.py: {bad}"


def test_every_loop_is_owner_only_until_an_adr_amendment_says_otherwise() -> None:
    """ADR 0002: only the execution-owner lease holder runs the loops."""
    assert {d.cls_name: d.ownership for d in worker_inventory()} == {
        cls: "owner_only" for _name, cls in _EXPECTED_WORKERS
    }


def test_a_worker_note_cannot_be_written_without_ownership() -> None:
    with pytest.raises(TypeError, match="ownership"):
        WorkerNote(description="x", tables=())  # type: ignore[call-arg]


def test_a_discovered_worker_with_no_note_has_no_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inventory reports the gap rather than guessing, so the gate above fails by name."""
    monkeypatch.delitem(_WORKER_NOTES, "JanitorTask")
    by_class = {d.cls_name: d for d in worker_inventory()}
    assert by_class["JanitorTask"].ownership is None
    assert by_class["JanitorTask"].description is None
    assert by_class["RefinementWorker"].ownership == "owner_only"


def test_a_note_with_an_unsupported_ownership_is_refused() -> None:
    with pytest.raises(ValueError, match="ownership must be one of"):
        WorkerNote(description="x", tables=(), ownership="whoever")  # type: ignore[arg-type]


@pytest.mark.skipif(not ADR_PATH.is_file(), reason="docs/ is not part of this installation")
def test_adr_0002_appendix_matches_the_inventory() -> None:
    adr = ADR_PATH.read_text(encoding="utf-8")
    section = adr.split("## Appendix: worker ownership classification", 1)[1]
    rows = re.findall(r"^\| `(\w+)` \| `(\w+)` \|", section, flags=re.M)
    assert dict(rows) == {d.cls_name: d.ownership for d in worker_inventory()}
    assert len(rows) == len({cls for cls, _ in rows}), "a loop is listed twice"
