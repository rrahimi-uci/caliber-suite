"""Unit tests for workflow queue depth + worker liveness.

The product review (``product-complete-report.md`` §6) recorded
"queue-depth/worker operations" and "worker liveness, queue lag" as missing: the
only liveness signal was a database ``SELECT 1``, so a dead worker with a
backlogged queue was indistinguishable from a healthy idle system.

:func:`caliber.observability.queue_health.collect_queue_health` derives that
signal from state the worker already persists. These tests pin the verdict
boundaries with an injected ``now`` so nothing depends on wall-clock timing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkflowRun
from caliber.observability.queue_health import collect_queue_health

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
LEASE = 60.0
MAX_AGE = 300.0


def _run(
    session: Session,
    run_id: str,
    status: str,
    *,
    queued_at: datetime | None = None,
    claimed_by: str | None = None,
    heartbeat: datetime | None = None,
) -> None:
    session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id="WF-1",
            workflow_version_id="WFV-1",
            status=status,
            queued_at=queued_at or NOW,
            claimed_by=claimed_by,
            last_heartbeat_at=heartbeat,
        )
    )
    session.commit()


def _collect(session: Session):
    return collect_queue_health(
        session, lease_seconds=LEASE, max_queue_age_seconds=MAX_AGE, now=NOW
    )


def test_empty_queue_is_healthy(db_session: Session) -> None:
    h = _collect(db_session)
    assert h.healthy is True
    assert (h.queued, h.running, h.workers_alive, h.stale_leases) == (0, 0, 0, 0)
    assert h.oldest_queued_age_seconds is None


def test_counts_queued_and_running_separately(db_session: Session) -> None:
    _run(db_session, "R-q1", "queued")
    _run(db_session, "R-q2", "queued")
    _run(db_session, "R-run", "running", claimed_by="w1", heartbeat=NOW)
    # A finished run must not be counted as queue depth.
    _run(db_session, "R-done", "completed")

    h = _collect(db_session)

    assert h.queued == 2
    assert h.running == 1
    assert h.workers_alive == 1


def test_fresh_heartbeat_counts_the_worker_as_alive(db_session: Session) -> None:
    _run(db_session, "R-1", "running", claimed_by="w1", heartbeat=NOW - timedelta(seconds=10))

    h = _collect(db_session)

    assert h.workers_alive == 1
    assert h.stale_leases == 0
    assert h.healthy is True
    assert h.newest_heartbeat_age_seconds == 10.0


def test_heartbeat_past_the_lease_is_a_stale_lease(db_session: Session) -> None:
    """A claimed run whose heartbeat stopped means the worker died mid-run."""
    _run(db_session, "R-1", "running", claimed_by="w1", heartbeat=NOW - timedelta(seconds=600))

    h = _collect(db_session)

    assert h.stale_leases == 1
    assert h.workers_alive == 0
    assert h.healthy is False
    assert any("past lease" in r for r in h.degraded_reasons)


def test_running_run_with_no_heartbeat_at_all_is_stale(db_session: Session) -> None:
    """Claimed but never beat -- the abandoned-run case, not a live worker."""
    _run(db_session, "R-1", "running", claimed_by="w1", heartbeat=None)

    h = _collect(db_session)

    assert h.workers_alive == 0
    assert h.stale_leases == 1
    assert h.healthy is False


def test_backlog_older_than_tolerance_is_degraded(db_session: Session) -> None:
    _run(db_session, "R-old", "queued", queued_at=NOW - timedelta(seconds=MAX_AGE + 60))

    h = _collect(db_session)

    assert h.oldest_queued_age_seconds == MAX_AGE + 60
    assert h.healthy is False
    assert any("waited" in r for r in h.degraded_reasons)


def test_backlog_within_tolerance_is_not_degraded(db_session: Session) -> None:
    """Busy is not the same as broken -- a recent backlog must stay healthy."""
    _run(db_session, "R-recent", "queued", queued_at=NOW - timedelta(seconds=MAX_AGE - 1))
    _run(db_session, "R-run", "running", claimed_by="w1", heartbeat=NOW)

    h = _collect(db_session)

    assert h.healthy is True
    assert h.degraded_reasons == []


def test_queued_with_no_worker_is_degraded(db_session: Session) -> None:
    """The signal the review asked for: nothing is consuming the queue."""
    _run(db_session, "R-q", "queued", queued_at=NOW - timedelta(seconds=5))

    h = _collect(db_session)

    assert h.queued == 1
    assert h.workers_alive == 0
    assert h.healthy is False
    assert any("no live worker" in r for r in h.degraded_reasons)


def test_distinct_claimants_are_counted_once_each(db_session: Session) -> None:
    _run(db_session, "R-1", "running", claimed_by="w1", heartbeat=NOW)
    _run(db_session, "R-2", "running", claimed_by="w1", heartbeat=NOW)
    _run(db_session, "R-3", "running", claimed_by="w2", heartbeat=NOW)

    h = _collect(db_session)

    assert h.running == 3
    assert h.workers_alive == 2


def test_to_dict_is_json_safe_and_complete(db_session: Session) -> None:
    _run(db_session, "R-1", "running", claimed_by="w1", heartbeat=NOW)
    payload = _collect(db_session).to_dict()
    assert set(payload) == {
        "healthy",
        "queued",
        "running",
        "workers_alive",
        "stale_leases",
        "oldest_queued_age_seconds",
        "newest_heartbeat_age_seconds",
        "degraded_reasons",
    }
    import json

    json.dumps(payload)  # must not raise
