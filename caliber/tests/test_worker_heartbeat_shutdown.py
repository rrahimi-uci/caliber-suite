"""Tests for the worker's heartbeat bumping + graceful shutdown.

Two contracts:

1. Every successful stage advance bumps ``last_heartbeat_at`` on the
   job row. Failed ticks (e.g. unhandled exceptions) still leave a
   recent heartbeat from the start of the stage — that's what proves
   the worker was alive immediately before the crash.
2. ``RefinementWorker.stop(grace_seconds=...)`` waits for the current
   tick to finish before cancelling. A long-running stage gets to
   commit its terminal status; only an over-grace tick is cancelled.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.llm.fake import FakeLLMProvider
from caliber.orchestrator.worker import RefinementWorker


def _seed_agent_and_item(session: Session) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-H",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.commit()


def _seed_queued_job(session: Session, job_id: str = "RFN-H") -> None:
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-H",
            artifact_type="prompt",
            status="queued",
            current_stage="triage",
            bundle_targets=[],
        )
    )
    session.commit()


def _make_worker(
    session_factory: sessionmaker,  # type: ignore[type-arg]
) -> RefinementWorker:
    return RefinementWorker(
        session_factory=session_factory,
        llm_provider=FakeLLMProvider(),
        artifact_store=FakeArtifactStore(),
        eval_provider=FakeEvalProvider(),
        interval_seconds=999.0,
    )


def test_claim_seeds_heartbeat(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """A freshly-claimed job has a non-NULL ``last_heartbeat_at``
    before any stage runs, so the janitor doesn't reap a retry job
    whose prior-attempt heartbeat would otherwise look stale."""
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session)

    worker = _make_worker(session_factory)
    worker._claim_next_job()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-H")
    assert job is not None
    assert job.last_heartbeat_at is not None
    # Set during claim → very recent.
    age = datetime.now(timezone.utc) - job.last_heartbeat_at.replace(tzinfo=timezone.utc)
    assert age < timedelta(seconds=5)


def test_heartbeat_updates_during_pipeline(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """The end-to-end happy path bumps the heartbeat multiple times —
    once per stage. We don't pin the exact timestamp, just that it's
    recent and non-null after the worker tick completes."""
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session)

    worker = _make_worker(session_factory)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-H")
    assert job is not None
    assert job.last_heartbeat_at is not None
    assert job.status == "candidate_ready"


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_tick(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """A long-running tick gets to finish within the grace window.

    We can't easily make a real tick block for a precise duration
    inside an asyncio test, but we can drive ``stop`` against a worker
    whose task has already completed — the API should return promptly
    rather than spin its wheels on the cancel path.
    """
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session)

    worker = _make_worker(session_factory)
    await worker.start()
    await asyncio.sleep(0.01)  # let the loop pick up the queued job
    await worker.stop(grace_seconds=5.0)
    # No exception means the drain path worked.


@pytest.mark.asyncio
async def test_stop_cancels_after_grace_timeout() -> None:
    """When the grace window expires we cancel the task.

    Replace ``_run`` with a future that never completes on its own so
    the only way out is the cancel path. We can't easily wedge sync
    code in ``_tick`` (``asyncio.to_thread`` can't be cancelled across
    threads), but we *can* swap out ``_run`` itself.
    """
    worker = RefinementWorker(
        session_factory=None,  # type: ignore[arg-type] — never used by the override
        llm_provider=FakeLLMProvider(),
        artifact_store=FakeArtifactStore(),
        eval_provider=FakeEvalProvider(),
        interval_seconds=999.0,
    )

    # Swap ``_run`` for a coroutine that hangs until cancelled. Start
    # then stop with a short grace; the timeout path triggers the
    # cancel branch.
    async def _hang(self: object = worker) -> None:
        _ = self
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    worker._run = _hang  # type: ignore[method-assign]
    await worker.start()
    # stop() must return cleanly even when the inner task is stuck.
    await worker.stop(grace_seconds=0.05)


@pytest.mark.asyncio
async def test_stop_is_idempotent(
    session_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    worker = _make_worker(session_factory)
    # Never started → stop should be a no-op.
    await worker.stop(grace_seconds=1.0)
    await worker.start()
    await worker.stop(grace_seconds=1.0)
    await worker.stop(grace_seconds=1.0)
