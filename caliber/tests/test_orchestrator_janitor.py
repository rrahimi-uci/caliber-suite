"""Tests for the stale-job janitor.

The janitor's contract is narrow: a job in ``status='running'`` whose
``last_heartbeat_at`` is older than ``stale_threshold_seconds`` is
marked ``failed`` with a diagnostic message, plus one audit row. The
tests here pin each branch of that contract.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.orchestrator.janitor import JanitorTask


def _seed_agent_and_item(session: Session) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="agent",
            experiment_id="exp",
            name="Agent",
            owner="@x",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-J",
            agent_id="agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.commit()


def _seed_job(
    session: Session,
    *,
    job_id: str,
    status: str = "running",
    stage: str = "evidence",
    last_heartbeat_at: datetime | None = None,
    updated_at_offset_seconds: float | None = None,
) -> CaliberRefinementJob:
    job = CaliberRefinementJob(
        job_id=job_id,
        agent_id="agent",
        primary_item_id="FB-J",
        artifact_type="prompt",
        status=status,
        current_stage=stage,
        bundle_targets=[],
        last_heartbeat_at=last_heartbeat_at,
    )
    session.add(job)
    session.flush()
    if updated_at_offset_seconds is not None:
        # SQLAlchemy's ``onupdate`` fires server-side; we set the column
        # explicitly so the test can simulate a NULL-heartbeat row whose
        # updated_at is older than the cutoff.
        job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=updated_at_offset_seconds)
    session.commit()
    return job


def test_reaps_running_job_with_stale_heartbeat(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(
        db_session,
        job_id="RFN-STALE",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=3600),
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-STALE")
    assert job is not None
    assert job.status == "failed"
    assert "heartbeat stale" in (job.error_message or "")
    # The current_stage at the time of crash is preserved for the audit.
    assert job.current_stage == "evidence"


def test_does_not_reap_fresh_heartbeat(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(
        db_session,
        job_id="RFN-FRESH",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-FRESH")
    assert job is not None
    assert job.status == "running"
    assert job.error_message is None


def test_ignores_terminal_jobs(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """A job that's already completed/failed/rejected should never be
    touched, regardless of how stale its heartbeat looks."""
    _seed_agent_and_item(db_session)
    _seed_job(
        db_session,
        job_id="RFN-DONE",
        status="completed",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(days=10),
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-DONE")
    assert job is not None
    assert job.status == "completed"


def test_null_heartbeat_with_stale_updated_at_is_reaped(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """Older jobs created before the heartbeat column existed have NULL
    ``last_heartbeat_at``; we fall back to ``updated_at`` so they're
    not stuck in ``running`` forever."""
    _seed_agent_and_item(db_session)
    _seed_job(
        db_session,
        job_id="RFN-LEGACY",
        last_heartbeat_at=None,
        updated_at_offset_seconds=3600,
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-LEGACY")
    assert job is not None
    assert job.status == "failed"
    assert "never" in (job.error_message or "")


def test_null_heartbeat_with_recent_updated_at_is_left_alone(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """In-flight job whose first heartbeat hasn't landed yet (worker
    just claimed it but the update statement hasn't committed) must not
    be reaped — the ``updated_at`` check is the safety net."""
    _seed_agent_and_item(db_session)
    _seed_job(
        db_session,
        job_id="RFN-FRESH-NULL",
        last_heartbeat_at=None,
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-FRESH-NULL")
    assert job is not None
    assert job.status == "running"


def test_writes_audit_row_on_reap(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(
        db_session,
        job_id="RFN-AUDIT",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    audit_rows = (
        db_session.query(CaliberAuditLog)
        .filter_by(action="reap_stale_job", entity_id="RFN-AUDIT")
        .all()
    )
    assert len(audit_rows) == 1
    details = audit_rows[0].details  # type: ignore[index]
    assert details["from_stage"] == "evidence"
    assert details["stale_threshold_seconds"] == 60


def test_multiple_stale_jobs_reaped_in_one_tick(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    for i in range(3):
        _seed_job(
            db_session,
            job_id=f"RFN-{i}",
            last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()

    db_session.expire_all()
    statuses = {
        job.status
        for job in db_session.query(CaliberRefinementJob).all()
        if job.job_id.startswith("RFN-")
    }
    assert statuses == {"failed"}


def test_idle_tick_when_no_stale_jobs(
    session_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    """The query short-circuits when there's nothing to reap — no DB
    writes, no audit rows."""
    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    janitor._tick()  # must not raise


def test_does_not_reap_when_heartbeat_refreshes_mid_sweep(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """TOCTOU regression: a heartbeat that lands between the janitor's
    SELECT and the per-row UPDATE must leave the job untouched
    (no ``failed`` status, no audit row, no metric).

    The race window:

    1. Janitor SELECTs jobs with stale heartbeats — gets RFN-RACE.
    2. *In a different session* the worker writes a fresh
       ``last_heartbeat_at``.
    3. Janitor attempts to UPDATE the row to ``failed``.

    Pre-fix, step 3 succeeded unconditionally (the in-session ORM
    mutation didn't re-check the heartbeat predicate). Post-fix, the
    atomic ``UPDATE ... WHERE last_heartbeat_at < :cutoff`` matches
    zero rows and the janitor cleanly skips it.

    We simulate the race by:
    a) Building a janitor whose ``_find_stale`` loads the candidate.
    b) Refreshing the heartbeat directly via SQL (separate session)
       between ``_find_stale`` and ``_reap``.
    """
    _seed_agent_and_item(db_session)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=600)
    _seed_job(
        db_session,
        job_id="RFN-RACE",
        last_heartbeat_at=stale_time,
    )

    janitor = JanitorTask(session_factory, stale_threshold_seconds=60.0)
    cutoff = datetime.now(timezone.utc) - janitor._stale_threshold

    with session_factory() as sweep_session:
        candidates = janitor._find_stale(sweep_session, cutoff)
        assert len(candidates) == 1
        assert candidates[0].job_id == "RFN-RACE"

        # Worker side: heartbeat lands between SELECT and UPDATE.
        with session_factory() as worker_session:
            worker_job = worker_session.get(CaliberRefinementJob, "RFN-RACE")
            assert worker_job is not None
            worker_job.last_heartbeat_at = datetime.now(timezone.utc)
            worker_session.commit()

        # Janitor proceeds with the now-stale candidate; the atomic
        # UPDATE-WHERE in ``_reap`` matches zero rows.
        janitor._reap(sweep_session, candidates[0], cutoff)
        sweep_session.commit()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-RACE")
    assert job is not None
    assert job.status == "running"  # not failed
    assert job.error_message is None
    audit_rows = (
        db_session.query(CaliberAuditLog)
        .filter_by(entity_id="RFN-RACE", action="reap_stale_job")
        .all()
    )
    assert audit_rows == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle(session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
    janitor = JanitorTask(session_factory, interval_seconds=999.0)
    await janitor.start()
    await asyncio.sleep(0)  # let the task start
    await janitor.stop()
    # Idempotent.
    await janitor.stop()


@pytest.mark.asyncio
async def test_start_twice_raises(session_factory: sessionmaker) -> None:  # type: ignore[type-arg]
    janitor = JanitorTask(session_factory)
    await janitor.start()
    try:
        with pytest.raises(RuntimeError, match=r"already running"):
            await janitor.start()
    finally:
        await janitor.stop()


# ---------------------------------------------------------------------------
# Jobs stranded in ``applying`` (alpha gap P0-2)
#
# Apply commits its claim (candidate_ready -> applying) before it calls the
# promoter. A process that dies in that window leaves a job nothing else would
# touch: the release reconciler scans release operations and none exists yet.
# ---------------------------------------------------------------------------

_APPLYING_STALE = 600.0


def _seed_applying_job(
    session: Session, *, job_id: str, age_seconds: float, with_operation: bool = False
) -> None:
    from caliber.db.models import CaliberApprovalRequest, CaliberReleaseOperation

    _seed_job(
        session,
        job_id=job_id,
        status="applying",
        stage="done",
        updated_at_offset_seconds=age_seconds,
    )
    session.add(
        CaliberApprovalRequest(
            approval_id=f"AP-{job_id}",
            job_id=job_id,
            agent_id="agent",
            status="approved",
            eval_results={},
            candidate_snapshot={"content": "x"},
            approved_by="@op",
        )
    )
    session.flush()
    if with_operation:
        session.add(
            CaliberReleaseOperation(
                operation_id=f"REL-{job_id}",
                operation_type="promote",
                resource_type="prompt",
                resource_name="agent",
                target_name="prod",
                version_after=2,
                actor="@op",
                approval_id=f"AP-{job_id}",
                status="applying",
            )
        )
    session.commit()


def _janitor(session_factory: sessionmaker) -> JanitorTask:  # type: ignore[type-arg]
    return JanitorTask(session_factory, applying_stale_seconds=_APPLYING_STALE)


def test_reverts_a_job_stranded_in_applying_with_no_release_operation(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_applying_job(db_session, job_id="RFN-STRANDED", age_seconds=3600)

    _janitor(session_factory)._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-STRANDED")
    assert job is not None
    assert job.status == "candidate_ready"
    audit = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == "revert_stale_applying")
        .one()
    )
    assert audit.entity_id == "RFN-STRANDED"
    assert audit.actor == "caliber.janitor"
    assert audit.details["applying_stale_seconds"] == int(_APPLYING_STALE)


def test_leaves_a_recent_applying_job_alone(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """A live Apply spends its time inside a provider call, so a young
    ``applying`` row must never be touched."""
    _seed_agent_and_item(db_session)
    _seed_applying_job(db_session, job_id="RFN-LIVE", age_seconds=30)

    _janitor(session_factory)._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-LIVE")
    assert job is not None and job.status == "applying"
    assert db_session.query(CaliberAuditLog).count() == 0


def test_never_touches_an_applying_job_that_has_a_release_operation(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """The reconciler owns a job whose release operation exists: the provider
    outcome may be indeterminate, and reverting here could mask a real release."""
    _seed_agent_and_item(db_session)
    _seed_applying_job(db_session, job_id="RFN-HAS-OP", age_seconds=7200, with_operation=True)

    _janitor(session_factory)._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-HAS-OP")
    assert job is not None and job.status == "applying"
    assert (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == "revert_stale_applying")
        .count()
        == 0
    )


def test_applying_sweep_is_idempotent_and_only_reverts_the_stranded_ones(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_applying_job(db_session, job_id="RFN-A1", age_seconds=3600)
    _seed_applying_job(db_session, job_id="RFN-A2", age_seconds=3600, with_operation=True)
    _seed_applying_job(db_session, job_id="RFN-A3", age_seconds=10)

    janitor = _janitor(session_factory)
    janitor._tick()
    janitor._tick()

    db_session.expire_all()
    statuses = {
        row.job_id: row.status
        for row in db_session.query(CaliberRefinementJob).order_by(CaliberRefinementJob.job_id)
    }
    assert statuses == {
        "RFN-A1": "candidate_ready",
        "RFN-A2": "applying",
        "RFN-A3": "applying",
    }
    assert (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.action == "revert_stale_applying")
        .count()
        == 1
    )
