"""Tests for the refinement worker.

The worker picks queued jobs and advances them through every stage the
orchestrator currently supports (triage → evidence → diagnosis →
candidate → eval as of Phase 2.9). It must:

* Advance happy-path jobs to the terminal ``status=candidate_ready, stage=done``
  (no approval row — promotion happens later via the Apply endpoint).
* Pick FIFO when multiple jobs are queued.
* Mark jobs as ``failed`` when a stage raises.
* Not pick up jobs that aren't ``queued``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberRegressionRun,
    CaliberVerificationItem,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import ScoreSet
from caliber.events.bus import EventBus
from caliber.llm.circuit_breaker import LLMCircuitOpenError
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import Diagnosis, LLMProviderError, LLMUsage
from caliber.orchestrator.worker import RefinementWorker


def _make_worker(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    *,
    llm_provider: FakeLLMProvider | None = None,
    artifact_store: FakeArtifactStore | None = None,
    eval_provider: FakeEvalProvider | None = None,
    interval_seconds: float = 999.0,
) -> RefinementWorker:
    return RefinementWorker(
        session_factory=session_factory,
        llm_provider=llm_provider or FakeLLMProvider(),
        artifact_store=artifact_store or FakeArtifactStore(),
        eval_provider=eval_provider or FakeEvalProvider(),
        interval_seconds=interval_seconds,
    )


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
    artifact_type: str = "prompt",
) -> None:
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-W",
            artifact_type=artifact_type,
            status=status,
            current_stage=stage,
            bundle_targets=[],
        )
    )
    session.commit()


def test_tick_advances_job_all_the_way_to_candidate_ready(
    session_factory: sessionmaker, db_session: Session
) -> None:
    """End-to-end: queued/triage → ... → candidate_ready/done."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    worker = _make_worker(session_factory)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "candidate_ready"
    assert job.current_stage == "done"
    assert job.attempt_count == 1
    assert job.diagnosis is not None
    assert job.candidate is not None
    assert job.eval_results is not None
    assert job.eval_results["gate"]["passed"] is True
    assert job.optimizer_type == "MetaPrompt"
    assert job.total_tokens > 0


def test_tick_creates_no_approval_request_on_pass(
    session_factory: sessionmaker, db_session: Session
) -> None:
    """A passing pipeline lands the job at candidate_ready — no approval row."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    worker = _make_worker(session_factory)
    worker._tick()

    approvals = (
        db_session.execute(
            select(CaliberApprovalRequest).where(CaliberApprovalRequest.job_id == "RFN-W")
        )
        .scalars()
        .all()
    )
    assert approvals == []
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "candidate_ready"
    assert job.candidate is not None
    assert job.diagnosis is not None


def test_tick_rejects_when_eval_gate_fails(
    session_factory: sessionmaker, db_session: Session
) -> None:
    """A poor candidate (overall below floor) lands the job in ``rejected``."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)
    eval_provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.30, dimensions={"factual": 0.30}),
        baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88}),
    )

    worker = _make_worker(session_factory, eval_provider=eval_provider)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "rejected"
    assert job.current_stage == "done"

    # No approval row was created — gate failed.
    approvals = db_session.execute(select(CaliberApprovalRequest)).scalars().all()
    assert approvals == []


def test_tick_uses_injected_artifact_store(
    session_factory: sessionmaker, db_session: Session
) -> None:
    """The artifact store's value flows into the candidate context."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)
    provider = FakeLLMProvider()
    store = FakeArtifactStore({"support-agent": "v4.1 active prompt"})
    eval_provider = FakeEvalProvider()

    worker = _make_worker(
        session_factory,
        llm_provider=provider,
        artifact_store=store,
        eval_provider=eval_provider,
    )
    worker._tick()

    assert len(provider.candidate_calls) == 1
    assert provider.candidate_calls[0].current_artifact_content == "v4.1 active prompt"
    assert len(eval_provider.calls) == 1
    assert eval_provider.calls[0].baseline_content == "v4.1 active prompt"


def test_tick_creates_required_regression_replay_run(
    session_factory: sessionmaker, db_session: Session
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    worker = _make_worker(session_factory)
    worker._tick()

    runs = db_session.execute(select(CaliberRegressionRun)).scalars().all()
    assert len(runs) == 1
    # No approval is created at eval time — the replay row is provenance-only.
    assert runs[0].approval_id is None
    assert runs[0].status == "passed"


def test_tick_uses_injected_llm_provider(
    session_factory: sessionmaker, db_session: Session
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session)
    canned = Diagnosis(root_cause="prompt missing tool directive", confidence=0.91)
    provider = FakeLLMProvider(diagnose_response=canned)

    worker = _make_worker(session_factory, llm_provider=provider)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.diagnosis is not None
    assert job.diagnosis["root_cause"] == "prompt missing tool directive"
    assert job.diagnosis["confidence"] == 0.91
    assert len(provider.calls) == 1


def test_tick_picks_oldest_queued_job(session_factory: sessionmaker, db_session: Session) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session, job_id="RFN-Z")  # created first
    _seed_job(db_session, job_id="RFN-A")  # created second

    worker = _make_worker(session_factory)
    worker._tick()

    db_session.expire_all()
    older = db_session.get(CaliberRefinementJob, "RFN-Z")
    newer = db_session.get(CaliberRefinementJob, "RFN-A")
    assert older is not None
    assert newer is not None
    # Older advanced all the way through the pipeline to candidate_ready.
    # Newer wasn't touched on this tick because the worker processes one job per tick.
    assert older.status == "candidate_ready"
    assert newer.status == "queued"


def test_tick_idle_when_no_queued_jobs(session_factory: sessionmaker, db_session: Session) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session, status="completed", stage="done")

    worker = _make_worker(session_factory)
    worker._tick()

    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "completed"


def test_tick_marks_failed_when_orphan_item(
    session_factory: sessionmaker, db_session: Session
) -> None:
    """A job pointing at a deleted item should fail cleanly, not crash."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)
    db_session.query(CaliberVerificationItem).filter_by(item_id="FB-W").delete()
    db_session.commit()

    worker = _make_worker(session_factory)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "failed"
    assert job.error_message is not None

    audit = (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.entity_id == "RFN-W",
                CaliberAuditLog.action == "fail_job",
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1


def test_tick_marks_failed_on_llm_provider_error(
    session_factory: sessionmaker, db_session: Session
) -> None:
    """LLM-provider failure marks the job failed with the provider message in error_message."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    def boom(_evidence: object) -> tuple[Diagnosis, LLMUsage]:
        raise LLMProviderError("rate limit exceeded")

    provider = FakeLLMProvider(diagnose_callable=boom)
    worker = _make_worker(session_factory, llm_provider=provider)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "failed"
    assert job.error_message is not None
    assert "rate limit" in job.error_message
    # Triage and evidence already committed, so the stage tracks diagnosis (where it failed).
    assert job.current_stage == "diagnosis"


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle(session_factory: sessionmaker) -> None:
    ticks: list[int] = []
    worker = _make_worker(session_factory, interval_seconds=0.01)
    worker._tick = lambda: ticks.append(1)  # type: ignore[method-assign]

    await worker.start()
    await asyncio.sleep(0.05)
    await worker.stop()
    assert len(ticks) >= 1


@pytest.mark.asyncio
async def test_start_twice_raises(session_factory: sessionmaker) -> None:
    worker = _make_worker(session_factory, interval_seconds=10.0)
    await worker.start()
    try:
        with pytest.raises(RuntimeError, match=r"already running"):
            await worker.start()
    finally:
        await worker.stop()


def test_worker_publishes_job_advanced_events_on_each_stage(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """Each stage the worker advances through publishes one
    ``job.advanced`` event to the injected bus."""
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    bus = EventBus()
    captured: list[dict[str, object]] = []
    bus.publish = captured.append  # type: ignore[method-assign]

    worker = RefinementWorker(
        session_factory=session_factory,
        llm_provider=FakeLLMProvider(),
        artifact_store=FakeArtifactStore(),
        eval_provider=FakeEvalProvider(),
        interval_seconds=999.0,
        event_bus=bus,
    )
    worker._tick()

    stages_seen = [e["completed_stage"] for e in captured if e["type"] == "job.advanced"]
    # The happy path runs triage → evidence → diagnosis → candidate → eval.
    assert "triage" in stages_seen
    assert "eval" in stages_seen


def test_tick_requeues_on_llm_circuit_open(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    """``LLMCircuitOpenError`` re-queues the job rather than failing it.

    This is the "defer jobs without consuming retry budget" semantic
    from parity checklist §5.24: when the LLM circuit is open, the
    job stays queued at its current stage and a ``defer_job`` audit
    row is recorded.
    """
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    def boom(_evidence: object) -> tuple[Diagnosis, LLMUsage]:
        raise LLMCircuitOpenError("circuit is open")

    provider = FakeLLMProvider(diagnose_callable=boom)
    worker = _make_worker(session_factory, llm_provider=provider)
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.status == "queued"
    # Stage didn't regress — it's exactly where the breaker tripped.
    assert job.current_stage == "diagnosis"
    # No mark-failed audit row was written.
    fail_rows = (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.entity_id == "RFN-W",
                CaliberAuditLog.action == "fail_job",
            )
        )
        .scalars()
        .all()
    )
    assert fail_rows == []
    # A defer_job audit row records the reason.
    defer_rows = (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.entity_id == "RFN-W",
                CaliberAuditLog.action == "defer_job",
            )
        )
        .scalars()
        .all()
    )
    assert len(defer_rows) == 1
    details = defer_rows[0].details or {}
    assert details["stage"] == "diagnosis"
    assert "circuit is open" in details["reason"]


def test_tick_publishes_job_deferred_event_on_circuit_open(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    def boom(_evidence: object) -> tuple[Diagnosis, LLMUsage]:
        raise LLMCircuitOpenError("circuit is open")

    bus = EventBus()
    captured: list[dict[str, object]] = []
    bus.publish = captured.append  # type: ignore[method-assign]

    provider = FakeLLMProvider(diagnose_callable=boom)
    worker = RefinementWorker(
        session_factory=session_factory,
        llm_provider=provider,
        artifact_store=FakeArtifactStore(),
        eval_provider=FakeEvalProvider(),
        interval_seconds=999.0,
        event_bus=bus,
    )
    worker._tick()

    types = [e["type"] for e in captured]
    assert "job.deferred" in types
    assert "job.failed" not in types


def test_worker_publishes_job_failed_on_unhandled_error(
    session_factory: sessionmaker,  # type: ignore[type-arg]
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session)
    # Remove the primary item so triage raises LookupError.
    db_session.query(CaliberVerificationItem).filter_by(item_id="FB-W").delete()
    db_session.commit()

    bus = EventBus()
    captured: list[dict[str, object]] = []
    bus.publish = captured.append  # type: ignore[method-assign]

    worker = RefinementWorker(
        session_factory=session_factory,
        llm_provider=FakeLLMProvider(),
        artifact_store=FakeArtifactStore(),
        eval_provider=FakeEvalProvider(),
        interval_seconds=999.0,
        event_bus=bus,
    )
    worker._tick()

    types = [e["type"] for e in captured]
    assert "job.failed" in types


def test_tick_binds_mlflow_run_and_flushes_spans_when_experiment_resolves(
    session_factory: sessionmaker,
    db_session: Session,
) -> None:
    _seed_agent_and_item(db_session)
    _seed_job(db_session)

    start_run_calls: list[dict[str, object]] = []
    span_names: list[str] = []
    tag_calls: list[dict[str, object]] = []
    dict_artifacts: list[tuple[str, dict[str, object]]] = []
    metric_calls: list[tuple[str, float]] = []
    flush_calls: list[bool] = []

    class _RunContext:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self._kwargs = kwargs
            self.info = SimpleNamespace(run_id="run-mlflow-1")

        def __enter__(self) -> _RunContext:
            start_run_calls.append(self._kwargs)
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = exc_type
            _ = exc
            _ = tb

    class _Span:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    @contextmanager
    def _start_span(
        *,
        name: str,
        span_type: str | None = None,
        attributes: dict[str, object] | None = None,
        trace_destination: object | None = None,
    ):
        _ = span_type
        _ = trace_destination
        span_names.append(name)
        span = _Span()
        if attributes:
            span.attributes.update(attributes)
        yield span

    fake_mlflow = SimpleNamespace(
        get_experiment_by_name=lambda name: (
            SimpleNamespace(experiment_id="123") if name == "exp" else None
        ),
        start_run=lambda **kwargs: _RunContext(kwargs),
        start_span=_start_span,
        set_tags=lambda tags: tag_calls.append(dict(tags)),
        log_dict=lambda payload, artifact_file: dict_artifacts.append(
            (artifact_file, dict(payload))
        ),
        log_metric=lambda key, value: metric_calls.append((str(key), float(value))),
        flush_trace_async_logging=lambda terminate=False: flush_calls.append(bool(terminate)),
    )

    worker = _make_worker(session_factory)
    worker._import_mlflow = lambda: fake_mlflow  # type: ignore[method-assign]
    worker._tick()

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-W")
    assert job is not None
    assert job.mlflow_run_id == "run-mlflow-1"

    assert len(start_run_calls) == 1
    assert start_run_calls[0]["experiment_id"] == "123"
    assert start_run_calls[0]["run_name"] == "caliber-refinement-RFN-W"
    assert "caliber.refinement_job" in span_names
    assert "caliber.stage.triage" in span_names
    assert any(name == "caliber/job_start.json" for name, _ in dict_artifacts)
    assert any(name == "caliber/job_end.json" for name, _ in dict_artifacts)
    assert any(key == "caliber.total_tokens" for key, _ in metric_calls)
    assert any(key == "caliber.cost_usd" for key, _ in metric_calls)
    assert flush_calls == [False]
    assert len(tag_calls) >= 1
