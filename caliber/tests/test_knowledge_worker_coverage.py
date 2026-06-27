"""Coverage tests for :mod:`caliber.knowledge.worker`.

These exercise the background knowledge-base worker's claim / tick /
lease-recovery / heartbeat machinery deterministically — without running a
real build (no embedder, no object store, no MLflow). We drive the sync
helpers directly against seeded rows and the async ``start``/``stop``
lifecycle against an overridden ``_run`` coroutine, mirroring the patterns
in ``tests/test_worker_heartbeat_shutdown.py`` and ``tests/test_worker_atomic_claim.py``.

Targets the previously-uncovered branches:

* ``start()`` already-running guard (line 52);
* ``stop()`` grace-timeout cancel path + ``_run`` CancelledError re-raise
  (lines 68-75, 88-89);
* ``_append_event`` sequence bookkeeping (lines 107-116);
* ``_recover_expired_leases`` re-queue loop (lines 141-170);
* ``_heartbeat_loop`` renew + exception-swallow (lines 223-226).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseRun,
    CaliberKnowledgeBaseRunEvent,
    CaliberKnowledgeBaseVersion,
)
from caliber.knowledge.worker import KnowledgeBaseWorker


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_worker(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    config: CaliberConfig,
) -> KnowledgeBaseWorker:
    # Fast interval keeps the async loop tests snappy; no object store client
    # is wired so a real build would be impossible — but these tests never let
    # ``_execute_run`` reach a build.
    fast = config.model_copy(update={"knowledge_build_worker_interval_seconds": 0.01})
    return KnowledgeBaseWorker(session_factory, config=fast, object_store_client=None)


def _seed_run(
    session: Session,
    *,
    kb_id: str = "KB-1",
    version_id: str = "KBV-1",
    run_id: str = "KBR-1",
    run_status: str = "queued",
    version_status: str = "queued",
    lease_expires_at: datetime | None = None,
    claimed_by: str | None = None,
    queued_at: datetime | None = None,
) -> None:
    """Seed a KB + version + run triple with the given run state."""
    session.add(
        CaliberKnowledgeBase(
            knowledge_base_id=kb_id,
            name="Docs",
            description="",
            owner="@test",
            status="active",
            source_bucket="docs-bucket",
            source_manifest=[],
            last_run_status=run_status,
        )
    )
    session.add(
        CaliberKnowledgeBaseVersion(
            knowledge_base_version_id=version_id,
            knowledge_base_id=kb_id,
            version_number=1,
            status=version_status,
            chunking_strategy="markdown",
            chunking_config={},
            graph_config={},
            embedding_model="sentence-transformers/all-MiniLM-L6-v2",
            source_manifest=[],
            output_bucket="docs-bucket",
            output_prefix=".caliber/knowledge-bases/KB-1",
        )
    )
    session.add(
        CaliberKnowledgeBaseRun(
            knowledge_base_run_id=run_id,
            knowledge_base_id=kb_id,
            knowledge_base_version_id=version_id,
            status=run_status,
            source_manifest=[],
            queued_at=queued_at if queued_at is not None else _utcnow(),
            claimed_by=claimed_by,
            lease_expires_at=lease_expires_at,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# _claim_next_run / _tick (the happy claim + empty queue branches)
# ---------------------------------------------------------------------------


def test_claim_next_run_transitions_queued_to_running(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """A queued run is claimed: status→running, lease + heartbeat stamped."""
    _seed_run(db_session)
    worker = _make_worker(session_factory, app_config)

    claimed = worker._claim_next_run()
    assert claimed == "KBR-1"

    db_session.expire_all()
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-1")
    assert run is not None
    assert run.status == "running"
    assert run.claimed_by  # the generated worker id
    assert run.claimed_at is not None
    assert run.last_heartbeat_at is not None
    assert run.lease_expires_at is not None


def test_claim_next_run_returns_none_when_empty(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    worker = _make_worker(session_factory, app_config)
    assert worker._claim_next_run() is None


def test_tick_returns_when_nothing_queued(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """``_tick`` with an empty queue runs lease-recovery then returns (no claim)."""
    worker = _make_worker(session_factory, app_config)
    # No rows → _claim_next_run None → early return before _execute_run.
    worker._tick()


# ---------------------------------------------------------------------------
# _recover_expired_leases (lines 141-170) + _append_event (lines 107-116)
# ---------------------------------------------------------------------------


def test_recover_expired_leases_requeues_stale_run(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """A ``running`` run whose lease expired is re-queued, the version reset
    to queued, the KB's last_run_status reset, and a ``build_requeued`` event
    appended with the lease-expired reason."""
    expired = _utcnow() - timedelta(minutes=5)
    _seed_run(
        db_session,
        run_status="running",
        version_status="processing",
        lease_expires_at=expired,
        claimed_by="some-dead-worker",
    )
    worker = _make_worker(session_factory, app_config)

    worker._recover_expired_leases()

    db_session.expire_all()
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-1")
    assert run is not None
    assert run.status == "queued"
    assert run.claimed_by is None
    assert run.claimed_at is None
    assert run.lease_expires_at is None
    assert run.last_heartbeat_at is None
    assert run.error_summary == "worker lease expired; build re-queued for recovery"

    version = db_session.get(CaliberKnowledgeBaseVersion, "KBV-1")
    assert version is not None
    assert version.status == "queued"
    assert version.error_summary is None
    assert version.completed_at is None

    kb = db_session.get(CaliberKnowledgeBase, "KB-1")
    assert kb is not None
    assert kb.last_run_status == "queued"

    events = (
        db_session.execute(
            select(CaliberKnowledgeBaseRunEvent)
            .where(CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == "KBR-1")
            .order_by(CaliberKnowledgeBaseRunEvent.sequence.asc())
        )
        .scalars()
        .all()
    )
    assert [e.event_type for e in events] == ["build_requeued"]
    requeued = events[0]
    assert requeued.sequence == 1  # first event → max(seq) was NULL → 0 + 1
    assert requeued.payload is not None
    assert requeued.payload["reason"] == "lease_expired"
    assert requeued.payload["worker_id"] == worker._worker_id


def test_recover_expired_leases_noop_when_no_expired(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """A running run with a lease that has NOT expired is left untouched
    (the ``if not rows: return`` early-out, no event written)."""
    future = _utcnow() + timedelta(minutes=10)
    _seed_run(
        db_session,
        run_status="running",
        version_status="processing",
        lease_expires_at=future,
        claimed_by="live-worker",
    )
    worker = _make_worker(session_factory, app_config)

    worker._recover_expired_leases()

    db_session.expire_all()
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-1")
    assert run is not None
    assert run.status == "running"  # unchanged
    assert run.claimed_by == "live-worker"

    events = (
        db_session.execute(
            select(CaliberKnowledgeBaseRunEvent).where(
                CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == "KBR-1"
            )
        )
        .scalars()
        .all()
    )
    assert events == []


def test_append_event_first_event_starts_at_one(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """With no prior events, ``max(sequence)`` is NULL so the first event
    lands at sequence 1 (``(current or 0) + 1``). This is the path
    ``_recover_expired_leases`` exercises (one event per commit)."""
    _seed_run(db_session)
    worker = _make_worker(session_factory, app_config)

    with session_factory() as session:
        worker._append_event(session, run_id="KBR-1", event_type="alpha", payload={"k": "v"})
        session.commit()

    db_session.expire_all()
    events = (
        db_session.execute(
            select(CaliberKnowledgeBaseRunEvent)
            .where(CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == "KBR-1")
            .order_by(CaliberKnowledgeBaseRunEvent.sequence.asc())
        )
        .scalars()
        .all()
    )
    assert [(e.sequence, e.event_type) for e in events] == [(1, "alpha")]
    assert events[0].payload == {"k": "v"}


def test_append_event_continues_from_existing_max(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """When prior events exist, ``_append_event`` reads ``max(sequence)`` and
    appends at max+1. Seed an existing sequence-3 event, then append once →
    the new event lands at sequence 4."""
    _seed_run(db_session)
    db_session.add(
        CaliberKnowledgeBaseRunEvent(
            knowledge_base_run_id="KBR-1",
            sequence=3,
            event_type="seeded",
            payload=None,
            created_at=_utcnow(),
        )
    )
    db_session.commit()

    worker = _make_worker(session_factory, app_config)
    with session_factory() as session:
        worker._append_event(session, run_id="KBR-1", event_type="next", payload=None)
        session.commit()

    db_session.expire_all()
    events = (
        db_session.execute(
            select(CaliberKnowledgeBaseRunEvent)
            .where(CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == "KBR-1")
            .order_by(CaliberKnowledgeBaseRunEvent.sequence.asc())
        )
        .scalars()
        .all()
    )
    assert [(e.sequence, e.event_type) for e in events] == [(3, "seeded"), (4, "next")]


# ---------------------------------------------------------------------------
# _heartbeat_loop (lines 223-226)
# ---------------------------------------------------------------------------


class _OneShotEvent:
    """A stand-in for ``threading.Event``: ``wait`` returns False on the first
    call (so the heartbeat loop body runs exactly once) and True thereafter
    (so the loop then exits). This makes the loop fully deterministic without
    depending on the 5s ``max(5, lease/3)`` wall-clock floor."""

    def __init__(self) -> None:
        self._calls = 0

    def wait(self, _timeout: float | None = None) -> bool:
        self._calls += 1
        return self._calls > 1


def test_renew_lease_bumps_heartbeat_and_lease(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """``_renew_lease`` updates the running run owned by this worker: one row
    matched, lease + heartbeat pushed forward."""
    _seed_run(
        db_session,
        run_status="running",
        version_status="processing",
        lease_expires_at=_utcnow() + timedelta(seconds=1),
    )
    worker = _make_worker(session_factory, app_config)
    with session_factory() as session:
        run = session.get(CaliberKnowledgeBaseRun, "KBR-1")
        assert run is not None
        run.claimed_by = worker._worker_id  # _renew_lease WHERE matches owner
        session.commit()

    db_session.expire_all()
    before = db_session.get(CaliberKnowledgeBaseRun, "KBR-1")
    assert before is not None
    old_lease = before.lease_expires_at

    rows = worker._renew_lease("KBR-1")
    assert rows == 1

    db_session.expire_all()
    after = db_session.get(CaliberKnowledgeBaseRun, "KBR-1")
    assert after is not None and after.lease_expires_at is not None and old_lease is not None
    assert after.lease_expires_at >= old_lease
    assert after.last_heartbeat_at is not None


def test_heartbeat_loop_renews_once_then_stops(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """One loop iteration calls ``_renew_lease`` for the run, then the event
    reports stopped and the loop exits cleanly."""
    worker = _make_worker(session_factory, app_config)
    seen: list[str] = []

    def _record(run_id: str) -> int:
        seen.append(run_id)
        return 1

    worker._renew_lease = _record  # type: ignore[method-assign]
    worker._heartbeat_loop("KBR-99", _OneShotEvent())  # type: ignore[arg-type]
    assert seen == ["KBR-99"]


def test_heartbeat_loop_swallows_renew_errors(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """If ``_renew_lease`` raises inside the loop, the exception is swallowed
    (logged at debug) and ``_heartbeat_loop`` returns without propagating."""
    worker = _make_worker(session_factory, app_config)
    calls: list[str] = []

    def _boom(run_id: str) -> int:
        calls.append(run_id)
        raise RuntimeError("transient DB failure")

    worker._renew_lease = _boom  # type: ignore[method-assign]
    # Must not raise — the except branch (lines 225-226) swallows it.
    worker._heartbeat_loop("KBR-99", _OneShotEvent())  # type: ignore[arg-type]
    assert calls == ["KBR-99"]


# ---------------------------------------------------------------------------
# async lifecycle: start guard (52) + stop cancel path (68-75) + _run (88-89)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_twice_raises_runtime_error(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """Calling ``start()`` while a task is already running raises."""
    worker = _make_worker(session_factory, app_config)

    # Override _run with a hang so the started task stays alive for the guard.
    async def _hang() -> None:
        await asyncio.Event().wait()

    worker._run = _hang  # type: ignore[method-assign]
    await worker.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await worker.start()
    finally:
        await worker.stop(grace_seconds=0.05)


@pytest.mark.asyncio
async def test_stop_cancels_after_grace_timeout(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """When the grace window expires, ``stop`` cancels the hung task and the
    ``_run`` CancelledError re-raise (lines 88-89) and the cancel branch
    (lines 68-75) both execute. ``stop`` must still return cleanly."""
    worker = _make_worker(session_factory, app_config)

    async def _hang() -> None:
        # Mirror the real _run: a CancelledError must propagate (re-raised).
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    worker._run = _hang  # type: ignore[method-assign]
    await worker.start()
    # Short grace → wait_for times out → cancel branch.
    await worker.stop(grace_seconds=0.05)
    assert worker._task is None


@pytest.mark.asyncio
async def test_stop_is_noop_when_never_started(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    worker = _make_worker(session_factory, app_config)
    # No task → early return, no error.
    await worker.stop(grace_seconds=1.0)
    assert worker._task is None


@pytest.mark.asyncio
async def test_run_loop_executes_real_tick_and_drains(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """Start the real ``_run`` loop with a queued run whose ``_execute_run`` is
    stubbed (no real build). The loop should claim + dispatch the run, then
    ``stop`` should drain cleanly within grace."""
    _seed_run(db_session)
    worker = _make_worker(session_factory, app_config)

    executed: list[str] = []

    def _fake_execute(run_id: str) -> None:
        executed.append(run_id)

    worker._execute_run = _fake_execute  # type: ignore[method-assign]

    await worker.start()
    # Give the loop a few ticks to claim and dispatch the seeded run.
    for _ in range(50):
        if executed:
            break
        await asyncio.sleep(0.01)
    await worker.stop(grace_seconds=5.0)

    assert executed == ["KBR-1"]
    db_session.expire_all()
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-1")
    assert run is not None
    # _claim_next_run moved it to running; the stubbed execute didn't complete it.
    assert run.status == "running"


@pytest.mark.asyncio
async def test_run_loop_survives_tick_exception(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """A ``_tick`` that raises is caught + logged inside ``_run`` (lines 84-85)
    and the loop keeps spinning; ``stop`` then drains it cleanly."""
    worker = _make_worker(session_factory, app_config)

    ticks = {"n": 0}

    def _boom_tick() -> None:
        ticks["n"] += 1
        raise RuntimeError("tick blew up")

    worker._tick = _boom_tick  # type: ignore[method-assign]

    await worker.start()
    for _ in range(50):
        if ticks["n"] >= 1:
            break
        await asyncio.sleep(0.01)
    await worker.stop(grace_seconds=5.0)

    # The loop must have invoked the failing tick at least once and survived.
    assert ticks["n"] >= 1
    assert worker._task is None


@pytest.mark.asyncio
async def test_real_run_reraises_on_direct_cancel(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """Drive the REAL ``_run`` (not an override) and cancel its task directly.

    This exercises the ``except asyncio.CancelledError: raise`` re-raise
    (lines 88-89). We make ``_tick`` a no-op so the loop just idles between
    intervals, then cancel the task and assert it surfaces ``CancelledError``.
    """
    worker = _make_worker(session_factory, app_config)
    worker._tick = lambda: None  # type: ignore[method-assign]

    await worker.start()
    task = worker._task
    assert task is not None
    await asyncio.sleep(0.02)  # let the loop reach its interval wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Reset bookkeeping so the worker can be torn down without surprises.
    worker._task = None


# ---------------------------------------------------------------------------
# _execute_run (lines 229-245): heartbeat thread spins up + joins around the
# service call, which we stub so no real build / object store is needed.
# ---------------------------------------------------------------------------


def test_execute_run_spawns_heartbeat_and_calls_service(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """``_execute_run`` starts a daemon heartbeat thread, calls
    ``service.execute_run`` with the worker id + a heartbeat callable, then
    stops + joins the thread in ``finally``."""
    worker = _make_worker(session_factory, app_config)

    received: dict[str, object] = {}

    def _fake_service_execute(
        run_id: str,
        *,
        worker_id: str | None = None,
        heartbeat: object = None,
    ) -> None:
        received["run_id"] = run_id
        received["worker_id"] = worker_id
        # The injected heartbeat must be callable; invoking it routes to
        # _renew_lease (which finds no matching row here and returns 0).
        assert callable(heartbeat)
        received["heartbeat_result"] = heartbeat()

    worker._service.execute_run = _fake_service_execute  # type: ignore[method-assign]

    worker._execute_run("KBR-1")

    assert received["run_id"] == "KBR-1"
    assert received["worker_id"] == worker._worker_id
    # No matching running row claimed by this worker → renew matched 0 rows.
    assert received["heartbeat_result"] == 0


def test_execute_run_joins_heartbeat_even_on_service_error(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    app_config: CaliberConfig,
) -> None:
    """If the service raises, ``_execute_run`` propagates it but the ``finally``
    still stops + joins the heartbeat thread (no leaked thread)."""
    worker = _make_worker(session_factory, app_config)

    def _boom(run_id: str, **_kw: object) -> None:
        raise RuntimeError("build exploded")

    worker._service.execute_run = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="build exploded"):
        worker._execute_run("KBR-1")

    # No daemon heartbeat thread should still be alive for this run.
    alive = [t for t in threading.enumerate() if "caliber-kb-heartbeat" in t.name]
    assert alive == []


# ---------------------------------------------------------------------------
# _recover_expired_leases partial branches: version missing (153->157) and
# knowledge_base missing (158->160) are skipped without error.
# ---------------------------------------------------------------------------


def test_recover_expired_leases_handles_missing_version_and_kb(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
    app_config: CaliberConfig,
) -> None:
    """A stale running run whose version + KB rows are absent is still
    re-queued; the ``if version is not None`` / ``if knowledge_base is not
    None`` guards skip the missing rows (partial branches) and the event is
    still appended."""
    expired = _utcnow() - timedelta(minutes=5)
    # Seed ONLY the run row — point it at non-existent version/KB ids.
    db_session.add(
        CaliberKnowledgeBaseRun(
            knowledge_base_run_id="KBR-ORPHAN",
            knowledge_base_id="KB-MISSING",
            knowledge_base_version_id="KBV-MISSING",
            status="running",
            source_manifest=[],
            queued_at=expired,
            claimed_by="dead-worker",
            lease_expires_at=expired,
        )
    )
    db_session.commit()

    worker = _make_worker(session_factory, app_config)
    worker._recover_expired_leases()  # must not raise

    db_session.expire_all()
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-ORPHAN")
    assert run is not None
    assert run.status == "queued"
    assert run.claimed_by is None

    events = (
        db_session.execute(
            select(CaliberKnowledgeBaseRunEvent).where(
                CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == "KBR-ORPHAN"
            )
        )
        .scalars()
        .all()
    )
    assert [e.event_type for e in events] == ["build_requeued"]
