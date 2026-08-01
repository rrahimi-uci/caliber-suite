"""Worker self-registration, so an *idle* worker's liveness is observable (L3).

The independent validation pass recorded:

    Worker heartbeats are inferred only from currently ``running`` rows. The encoded
    empty-queue contract declares ``(queued=0, running=0, workers_alive=0)`` healthy,
    so an idle dead worker and a healthy idle worker are indistinguishable.

These tests pin the inverted evidence direction: each worker writes its own row every
poll cycle, so liveness no longer depends on work existing. Four states must be
distinguishable, because an operator does something different about each:

* **fresh registration** — healthy, no work needed to prove it;
* **stale registration** — a worker died, and which one is named;
* **no registration at all** — a deployment that never started a worker; and
* **queue deliberately disabled** — no worker is expected, so silence is correct.

An injected ``now`` keeps every boundary deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkerHeartbeat, CaliberWorkflowRun
from caliber.observability.queue_health import collect_queue_health
from caliber.observability.worker_registry import (
    KIND_WORKFLOW_RUN,
    deregister,
    list_workers,
    new_worker_id,
    record_heartbeat,
    stale_after_seconds,
)

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
LEASE = 60.0
MAX_AGE = 300.0
INTERVAL = 5.0


def _collect(session: Session, *, now: datetime = NOW, require_worker: bool = True):
    return collect_queue_health(
        session,
        lease_seconds=LEASE,
        max_queue_age_seconds=MAX_AGE,
        now=now,
        worker_interval_seconds=INTERVAL,
        require_worker=require_worker,
    )


# ---------------------------------------------------------------------------
# Staleness threshold
# ---------------------------------------------------------------------------


def test_staleness_is_derived_from_the_cadence_it_measures() -> None:
    """A separately configured threshold silently becomes wrong when an operator
    retunes the poll interval, so it is derived instead."""
    # Three cycles tolerates two consecutive misses without crying wolf.
    assert stale_after_seconds(20.0) == 60.0
    # The lease is a floor: a worker mid-run reports through its lease loop, not its
    # poll loop, so a long node must not look like a stalled worker.
    assert stale_after_seconds(5.0, lease_seconds=120.0) == 120.0
    # And an absolute floor, so a very tight interval cannot make every worker look
    # dead between two ticks.
    assert stale_after_seconds(1.0) == 30.0


# ---------------------------------------------------------------------------
# Registration lifecycle
# ---------------------------------------------------------------------------


def test_recording_a_heartbeat_creates_then_updates_one_row(db_session: Session) -> None:
    record_heartbeat(db_session, worker_id="w-1", now=NOW)
    record_heartbeat(db_session, worker_id="w-1", now=NOW + timedelta(seconds=5))

    rows = db_session.query(CaliberWorkerHeartbeat).all()
    assert len(rows) == 1, "one row per worker identity, not one per tick"
    assert rows[0].ticks == 2, "the tick counter distinguishes a wedged loop from an idle one"


def test_an_idle_worker_is_alive_with_an_empty_queue(db_session: Session) -> None:
    """The core L3 property: no runs at all, and the worker is still provably alive."""
    record_heartbeat(db_session, worker_id="w-idle", now=NOW)

    health = _collect(db_session)

    assert health.queued == 0
    assert health.running == 0
    assert health.workers_alive == 1
    assert health.healthy is True, health.degraded_reasons


def test_a_dead_idle_worker_is_reported_and_named(db_session: Session) -> None:
    """The other half: same empty queue, stale heartbeat, and now it is a fault.

    Before this the two readings were byte-identical.
    """
    record_heartbeat(db_session, worker_id="w-dead", now=NOW - timedelta(seconds=600))

    health = _collect(db_session)

    assert health.healthy is False
    assert health.workers_alive == 0
    assert health.workers_stale == 1
    assert any("w-dead" in reason for reason in health.degraded_reasons), health.degraded_reasons


def test_never_registered_and_went_stale_are_distinct_verdicts(db_session: Session) -> None:
    """An operator restarts a worker in one case and looks at deployment in the
    other, so the two must not collapse into one message."""
    never = _collect(db_session)
    assert any("has registered" in reason for reason in never.degraded_reasons)

    record_heartbeat(db_session, worker_id="w-x", now=NOW - timedelta(seconds=600))
    stale = _collect(db_session)
    assert any("stopped reporting" in reason for reason in stale.degraded_reasons)


def test_deregistering_a_clean_shutdown_is_not_an_outage(db_session: Session) -> None:
    record_heartbeat(db_session, worker_id="w-bye", now=NOW)
    deregister(db_session, worker_id="w-bye")

    assert db_session.query(CaliberWorkerHeartbeat).count() == 0
    # With the queue disabled (synchronous execution) the absence is expected and
    # must not be reported. A graceful stop leaving a stale row would have made every
    # planned deploy look like a failure.
    assert _collect(db_session, require_worker=False).healthy is True


def test_stale_workers_are_listed_not_filtered_out(db_session: Session) -> None:
    """Dropping stale rows would restore the silence this registry removes."""
    record_heartbeat(db_session, worker_id="w-live", now=NOW)
    record_heartbeat(db_session, worker_id="w-gone", now=NOW - timedelta(seconds=600))

    records = list_workers(
        db_session,
        kind=KIND_WORKFLOW_RUN,
        stale_after=stale_after_seconds(INTERVAL, LEASE),
        now=NOW,
    )
    by_id = {record.worker_id: record for record in records}
    assert set(by_id) == {"w-live", "w-gone"}
    assert by_id["w-live"].alive is True
    assert by_id["w-gone"].alive is False
    assert by_id["w-gone"].heartbeat_age_seconds == 600.0


def test_a_fresh_claim_counts_as_alive_even_without_a_registration(
    db_session: Session,
) -> None:
    """The two signals are a union, not a choice.

    During a rolling upgrade an older worker may still hold a fresh claim while no
    longer writing registrations; counting only registrations would report that live
    worker as absent.
    """
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="R-1",
            workflow_id="WF-1",
            workflow_version_id="WFV-1",
            status="running",
            queued_at=NOW,
            claimed_by="legacy-worker",
            last_heartbeat_at=NOW,
        )
    )
    db_session.commit()

    health = _collect(db_session)

    assert health.workers_alive == 1
    assert health.healthy is True, health.degraded_reasons


def test_the_same_worker_is_not_double_counted(db_session: Session) -> None:
    """Registered *and* holding a claim is one worker, not two — otherwise a single
    busy worker would inflate the count and mask a genuinely missing one."""
    record_heartbeat(db_session, worker_id="w-1", now=NOW)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="R-1",
            workflow_id="WF-1",
            workflow_version_id="WFV-1",
            status="running",
            queued_at=NOW,
            claimed_by="w-1",
            last_heartbeat_at=NOW,
        )
    )
    db_session.commit()

    assert _collect(db_session).workers_alive == 1


def test_a_heartbeat_failure_never_breaks_the_caller(db_session: Session) -> None:
    """Losing an observability write is a nuisance; failing the tick because the
    observability table is unavailable would be an outage this module *caused*."""

    class _Boom(Session):
        pass

    class _BrokenSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("database is gone")

        def rollback(self) -> None:
            pass

    record_heartbeat(_BrokenSession(), worker_id="w-1", now=NOW)  # must not raise

    # And the session stays usable afterwards, which is why the rollback is there.
    record_heartbeat(db_session, worker_id="w-2", now=NOW)
    assert db_session.query(CaliberWorkerHeartbeat).count() == 1


def test_workers_appear_in_the_serialized_payload(db_session: Session) -> None:
    """``/system/queue`` renders this dict, so per-worker detail has to survive
    serialization rather than only existing on the dataclass."""
    record_heartbeat(db_session, worker_id="w-1", now=NOW)
    payload = _collect(db_session).to_dict()

    assert payload["workers_alive"] == 1
    assert payload["workers_stale"] == 0
    assert payload["workers"][0]["worker_id"] == "w-1"
    assert payload["workers"][0]["alive"] is True
    assert payload["workers"][0]["ticks"] == 1


# ---------------------------------------------------------------------------
# Worker id uniqueness across processes
# ---------------------------------------------------------------------------


def test_worker_id_differs_across_processes_for_the_same_object() -> None:
    """The regression this exists for.

    ``worker_id`` used to be ``f"...-{id(self):x}"``. Under a multi-worker server
    every process builds the same loops in the same order, so the addresses match,
    and the id — a durable primary key in a table shared by all of them — collided.
    Holding the instance fixed and varying only the pid is exactly that scenario.
    """
    sentinel = object()

    with mock.patch("caliber.observability.worker_registry.os.getpid", return_value=111):
        first = new_worker_id("workflow-run-worker", sentinel)
    with mock.patch("caliber.observability.worker_registry.os.getpid", return_value=222):
        second = new_worker_id("workflow-run-worker", sentinel)

    assert first != second, "same object in two processes must not share a worker id"
    assert "111" in first and "222" in second


def test_worker_id_differs_for_two_instances_in_one_process() -> None:
    """The pid alone is not enough: the tests construct several workers of one kind
    inside a single process, and those must not collide either."""
    assert new_worker_id("knowledge-build-worker", object()) != new_worker_id(
        "knowledge-build-worker", object()
    )


def test_worker_id_keeps_its_kind_prefix() -> None:
    """Operators read these ids straight off the Services page, so the prefix has to
    survive — an opaque id would name the outage without naming the worker."""
    assert new_worker_id("workflow-run-worker", object()).startswith("workflow-run-worker-")


def test_two_processes_register_two_rows_not_one(db_session: Session) -> None:
    """The observable symptom: with colliding ids the second INSERT violated
    ``caliber_worker_heartbeats_pkey``, so N live workers reported as one."""
    sentinel = object()
    with mock.patch("caliber.observability.worker_registry.os.getpid", return_value=111):
        a = new_worker_id("workflow-run-worker", sentinel)
    with mock.patch("caliber.observability.worker_registry.os.getpid", return_value=222):
        b = new_worker_id("workflow-run-worker", sentinel)

    record_heartbeat(db_session, worker_id=a, kind=KIND_WORKFLOW_RUN, now=NOW)
    record_heartbeat(db_session, worker_id=b, kind=KIND_WORKFLOW_RUN, now=NOW)

    assert db_session.query(CaliberWorkerHeartbeat).count() == 2
    assert _collect(db_session).workers_alive == 2
