"""Workflow queue depth and worker liveness, derived from durable run state.

The product review recorded "queue-depth/worker operations" and "worker liveness,
queue lag" as missing operational signals: ``/health`` proves only that the API
process and its database answer, and ``/readiness`` reports which providers are
*configured* rather than whether anything is actually working. Nothing exposed
whether the run queue was draining or whether a worker was alive.

No new schema is needed. :class:`~caliber.db.models.CaliberWorkflowRun` already
records everything required — ``status``, ``queued_at``, ``claimed_by``,
``claimed_at``, ``lease_expires_at``, ``last_heartbeat_at`` — and the table
already carries the two composite indexes these queries sort on
(``ix_workflow_runs_queue_claim`` and ``ix_workflow_runs_lease``). This module is
therefore read-only: it turns state the worker is already writing into an
operational verdict.

Deliberate boundaries:

* This describes the **workflow run** queue only. Knowledge-base builds and Aria
  plans have their own workers and are not aggregated here.
* ``workers_alive`` counts workers that have **registered and are reporting** (see
  :mod:`caliber.observability.worker_registry`), falling back to distinct claimants
  of running rows. With the shipped single-container topology that is 0 or 1; it is
  written as a count so a future multi-worker deployment reports usefully.
* A degraded verdict is an operational signal, not an error. Callers decide
  whether to fail a readiness probe on it.

The empty-queue case deserves its own note, because it was previously wrong.
``workers_alive`` was inferred solely from claimed ``running`` rows, so with nothing
queued and nothing running the reading was ``(0, 0, 0)`` — encoded as healthy — and a
dead idle worker was indistinguishable from a healthy idle one. Workers now register
their own heartbeat every poll cycle, so an idle queue with no live registration is
reported as degraded *before* a backlog forms. A deployment that runs no worker at
all (synchronous execution) is excluded by the caller via
``workflow_run_queue_enabled``, so this does not fire where there is deliberately no
worker to find.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from caliber.db.models import CaliberWorkflowRun
from caliber.observability.worker_registry import (
    KIND_WORKFLOW_RUN,
    list_workers,
    stale_after_seconds,
)

# Run statuses that occupy the queue. Kept local rather than imported from the
# worker module so this stays a leaf dependency importable from route code.
QUEUED = "queued"
RUNNING = "running"

# A claimed run whose heartbeat is older than its lease is presumed abandoned:
# the janitor requeues these, so a non-zero count means either a worker died or
# the janitor is not running.
_STALE_LEASE_GRACE = timedelta(seconds=5)


def _aware(value: datetime | None) -> datetime | None:
    """Normalize a DB-roundtripped timestamp to UTC-aware (SQLite drops tzinfo)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class QueueHealth:
    """A point-in-time read of workflow queue depth and worker liveness."""

    queued: int = 0
    running: int = 0
    workers_alive: int = 0
    stale_leases: int = 0
    oldest_queued_age_seconds: float | None = None
    newest_heartbeat_age_seconds: float | None = None
    degraded_reasons: list[str] = field(default_factory=list)
    #: Workers that registered but stopped reporting. Distinct from "never
    #: registered": one means a worker died, the other means none ever started, and
    #: an operator does different things about each.
    workers_stale: int = 0
    #: Per-worker detail, newest heartbeat first. Empty when no worker has ever
    #: registered — which is itself the signal, not missing data.
    workers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.degraded_reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "queued": self.queued,
            "running": self.running,
            "workers_alive": self.workers_alive,
            "workers_stale": self.workers_stale,
            "workers": list(self.workers),
            "stale_leases": self.stale_leases,
            "oldest_queued_age_seconds": self.oldest_queued_age_seconds,
            "newest_heartbeat_age_seconds": self.newest_heartbeat_age_seconds,
            "degraded_reasons": list(self.degraded_reasons),
        }


def collect_queue_health(
    session: Any,
    *,
    lease_seconds: float,
    max_queue_age_seconds: float,
    now: datetime | None = None,
    worker_interval_seconds: float = 0.0,
    require_worker: bool = True,
) -> QueueHealth:
    """Summarize the workflow run queue and its workers.

    ``lease_seconds`` defines both worker freshness and lease staleness, so a
    single configured value (``CALIBER_WORKFLOW_RUN_LEASE_SECONDS``) governs the
    verdict rather than a second threshold that could drift from the worker's own
    renewal cadence.

    ``max_queue_age_seconds`` is the backlog tolerance: a run waiting longer than
    this marks the queue degraded, which is what distinguishes "busy" from
    "nothing is consuming the queue".

    ``worker_interval_seconds`` is the worker's poll cadence, used to judge heartbeat
    staleness against the loop actually being measured.

    ``require_worker`` says whether a missing registration is a fault. It is for a
    queue-backed deployment — the point of the registry is to catch a dead idle
    worker. It is not when execution is synchronous, where there is deliberately no
    worker and reporting its absence would be a permanent false alarm.
    """
    now = now or datetime.now(timezone.utc)
    lease_window = timedelta(seconds=max(lease_seconds, 0.0))

    counts = dict(
        session.execute(
            select(CaliberWorkflowRun.status, func.count())
            .where(CaliberWorkflowRun.status.in_((QUEUED, RUNNING)))
            .group_by(CaliberWorkflowRun.status)
        ).all()
    )
    queued = int(counts.get(QUEUED, 0))
    running = int(counts.get(RUNNING, 0))

    oldest_queued = _aware(
        session.execute(
            select(func.min(CaliberWorkflowRun.queued_at)).where(
                CaliberWorkflowRun.status == QUEUED
            )
        ).scalar()
    )
    oldest_age = (now - oldest_queued).total_seconds() if oldest_queued else None

    # Worker liveness: distinct claimants of running rows whose heartbeat is
    # inside the lease window. A claimant with no heartbeat at all does not count
    # as alive — that is precisely the abandoned-run case.
    running_rows = (
        session.execute(
            select(CaliberWorkflowRun.claimed_by, CaliberWorkflowRun.last_heartbeat_at).where(
                CaliberWorkflowRun.status == RUNNING
            )
        )
        .tuples()
        .all()
    )
    fresh_workers: set[str] = set()
    newest_beat: datetime | None = None
    stale = 0
    for claimed_by, heartbeat in running_rows:
        beat = _aware(heartbeat)
        if beat is not None and (newest_beat is None or beat > newest_beat):
            newest_beat = beat
        if beat is not None and now - beat <= lease_window + _STALE_LEASE_GRACE:
            if claimed_by:
                fresh_workers.add(claimed_by)
        else:
            stale += 1

    # Registered workers: direct evidence, independent of whether work exists. This
    # is what makes an idle-but-dead worker detectable at all.
    registered = list_workers(
        session,
        kind=KIND_WORKFLOW_RUN,
        stale_after=stale_after_seconds(worker_interval_seconds, lease_seconds),
        now=now,
    )
    live_registered = [record for record in registered if record.alive]
    stale_registered = [record for record in registered if not record.alive]
    # The union, not a choice between them: a worker that registered *or* holds a
    # fresh claim is alive, and counting only one source would under-report during an
    # upgrade where an old worker still holds a claim but no longer registers.
    alive_ids = {record.worker_id for record in live_registered} | fresh_workers

    reasons: list[str] = []
    if oldest_age is not None and oldest_age > max_queue_age_seconds:
        reasons.append(
            f"oldest queued run has waited {oldest_age:.0f}s "
            f"(> {max_queue_age_seconds:.0f}s tolerance)"
        )
    if queued and not alive_ids and not running:
        reasons.append(f"{queued} run(s) queued with no live worker")
    if stale:
        reasons.append(f"{stale} running run(s) past lease with no fresh heartbeat")
    if stale_registered:
        # Named, because "which worker stopped" is the actionable part.
        names = ", ".join(sorted(record.worker_id for record in stale_registered))
        reasons.append(f"{len(stale_registered)} registered worker(s) stopped reporting: {names}")
    if require_worker and not alive_ids:
        # The closure of the empty-queue defect: no live worker is degraded even with
        # an empty queue. Distinguishing "never registered" from "went stale" matters
        # — the first is a deployment that never started a worker, the second is one
        # that died — and the stale case already has its own reason above.
        if not registered:
            reasons.append("no workflow-run worker has registered a heartbeat")
        elif not live_registered and not fresh_workers:
            reasons.append("no workflow-run worker is reporting a fresh heartbeat")

    return QueueHealth(
        queued=queued,
        running=running,
        workers_alive=len(alive_ids),
        workers_stale=len(stale_registered),
        workers=[record.to_dict() for record in registered],
        stale_leases=stale,
        oldest_queued_age_seconds=oldest_age,
        newest_heartbeat_age_seconds=(now - newest_beat).total_seconds() if newest_beat else None,
        degraded_reasons=reasons,
    )


def collect_queue_health_for_config(
    session: Any, config: Any, *, now: datetime | None = None
) -> QueueHealth:
    """``collect_queue_health`` with every threshold read from ``config``.

    Three routes need this identical derivation (``/readiness``, ``/system/queue``,
    ``/system/alerts``), and they must not disagree: two endpoints reporting different
    verdicts for the same queue in the same second is worse than either being wrong.
    Centralizing it also means a newly added threshold reaches all three at once
    instead of the one whose call site was remembered.
    """
    return collect_queue_health(
        session,
        lease_seconds=float(getattr(config, "workflow_run_lease_seconds", 60.0) or 60.0),
        max_queue_age_seconds=float(
            getattr(config, "workflow_queue_max_age_seconds", 300.0) or 300.0
        ),
        worker_interval_seconds=float(
            getattr(config, "workflow_run_worker_interval_seconds", 0.0) or 0.0
        ),
        require_worker=bool(getattr(config, "workflow_run_queue_enabled", False)),
        now=now,
    )


__all__ = [
    "QUEUED",
    "RUNNING",
    "QueueHealth",
    "collect_queue_health",
    "collect_queue_health_for_config",
]
