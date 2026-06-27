"""Edge-case tests for ``RefinementWorker`` uncovered branches.

Covers: _advance_job LookupError, loop exhaustion, _requeue_for_circuit
non-running guard, _mark_failed non-running guard, EvalProviderError path.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import EvalProviderError
from caliber.llm.fake import FakeLLMProvider
from caliber.orchestrator.worker import RefinementWorker


def _make_worker(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    **overrides: object,
) -> RefinementWorker:
    defaults: dict[str, object] = {
        "session_factory": session_factory,
        "llm_provider": FakeLLMProvider(),
        "artifact_store": FakeArtifactStore(),
        "eval_provider": FakeEvalProvider(),
        "interval_seconds": 999.0,
    }
    defaults.update(overrides)
    return RefinementWorker(**defaults)  # type: ignore[arg-type]


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
            item_id="FB-W",
            agent_id="support-agent",
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
    job_id: str = "RFN-W",
    status: str = "queued",
    stage: str = "triage",
) -> None:
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-W",
            artifact_type="prompt",
            status=status,
            current_stage=stage,
            bundle_targets=[],
        )
    )
    session.commit()


def test_advance_job_handles_vanished_job(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """When _current_stage raises LookupError (job deleted), _advance_job returns cleanly."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    worker = _make_worker(session_factory)
    # Delete the job after claim so _current_stage raises LookupError.
    db_session.query(CaliberRefinementJob).filter_by(job_id="RFN-W").delete()
    db_session.commit()

    # _advance_job should return without error when job is gone.
    worker._advance_job("RFN-W", trace_id="tr-vanished")


def test_advance_job_loop_exhaustion(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """When stages don't advance current_stage, the loop is bounded by _MAX_STAGES_PER_JOB."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session, status="running", stage="triage")

    worker = _make_worker(session_factory)

    # Patch _run_stage to always succeed but never advance the stage.
    with patch.object(worker, "_run_stage", return_value=True):
        worker._advance_job("RFN-W", trace_id="tr-exhaustion")

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "failed"
    assert "max iterations" in (job.error_message or "")


def test_requeue_for_circuit_skips_non_running_job(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """_requeue_for_circuit is a no-op when the job is not running."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session, status="failed", stage="diagnosis")

    worker = _make_worker(session_factory)
    worker._requeue_for_circuit("RFN-W", "diagnosis", "test")

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "failed"  # unchanged


def test_requeue_for_circuit_skips_missing_job(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """_requeue_for_circuit is a no-op when the job doesn't exist."""
    worker = _make_worker(session_factory)
    # Should not raise.
    worker._requeue_for_circuit("RFN-GONE", "triage", "test")


def test_mark_failed_skips_non_running_job(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """_mark_failed is a no-op when the job is not running."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session, status="candidate_ready", stage="done")

    worker = _make_worker(session_factory)
    worker._mark_failed("RFN-W", "should not apply")

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "candidate_ready"  # unchanged


def test_mark_failed_skips_missing_job(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """_mark_failed is a no-op when the job doesn't exist."""
    worker = _make_worker(session_factory)
    # Should not raise.
    worker._mark_failed("RFN-GONE", "test")


def test_tick_marks_failed_on_eval_provider_error(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """EvalProviderError in a stage fails the job with a descriptive message."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    def boom(*_a: object, **_kw: object) -> object:
        raise EvalProviderError("eval service down")

    provider = FakeEvalProvider(eval_callable=boom)
    worker = _make_worker(session_factory, eval_provider=provider)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "failed"
    assert "eval provider error" in (job.error_message or "")


def test_heartbeat_swallows_db_errors(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """_heartbeat swallows exceptions so a transient DB error doesn't crash the stage."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session, status="running", stage="triage")

    worker = _make_worker(session_factory)

    # Create a broken factory that raises inside the heartbeat.
    def broken_factory() -> object:
        raise RuntimeError("transient DB error")

    original_factory = worker._session_factory
    worker._session_factory = broken_factory  # type: ignore[assignment]

    # Should not raise — the exception is swallowed.
    worker._heartbeat("RFN-W")

    # Restore for cleanup.
    worker._session_factory = original_factory


def test_max_stages_per_job_scales_with_refinement_iterations(
    session_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Regression (#15): the per-advance stage cap must grow with
    refinement_max_iterations so a legitimate self-correction loop isn't falsely
    failed as 'stage loop exceeded'. A fresh job uses ~5 stages and each retry
    adds 2, so M retries need 5 + 2*(M+1) (floored at 16)."""
    from caliber.config import CaliberConfig

    low = _make_worker(session_factory, config=CaliberConfig(refinement_max_iterations=1))
    high = _make_worker(session_factory, config=CaliberConfig(refinement_max_iterations=6))
    none_cfg = _make_worker(session_factory)

    assert low._max_stages_per_job() == 16  # floor (5 + 2*2 = 9 < 16)
    assert high._max_stages_per_job() == 19  # 5 + 2*(6+1) = 19 > 16
    assert none_cfg._max_stages_per_job() == 16  # no config → floor
