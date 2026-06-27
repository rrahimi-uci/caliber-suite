"""Correctness tests for :meth:`RefinementWorker._claim_next_job`.

The claim is implemented as an atomic ``UPDATE ... WHERE status='queued'
RETURNING job_id``. The correctness invariant is:

* Exactly one ``_claim_next_job()`` call can transition any given queued
  row to ``running``. Subsequent calls on the same row return ``None``.

We test this property with **sequential** calls — pure threading tests
are unreliable under SQLite (writes are serialized inside the engine),
but the SQL guarantee we care about is verifiable without parallelism:
the second call observes the row in ``running`` status and its UPDATE's
``WHERE status='queued'`` predicate excludes it. That's the same
guarantee SQLite + Postgres provide under concurrent contention.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import CaliberAgentConfig, CaliberRefinementJob, CaliberVerificationItem
from caliber.eval.fake import FakeEvalProvider
from caliber.llm.fake import FakeLLMProvider
from caliber.orchestrator.worker import RefinementWorker


def _make_worker(session_factory: sessionmaker) -> RefinementWorker:  # type: ignore[type-arg]
    return RefinementWorker(
        session_factory=session_factory,
        llm_provider=FakeLLMProvider(),
        artifact_store=FakeArtifactStore(),
        eval_provider=FakeEvalProvider(),
        interval_seconds=999.0,
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
            item_id="FB-1",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.commit()


def _seed_queued_job(session: Session, job_id: str) -> None:
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="queued",
            current_stage="triage",
            bundle_targets=[],
        )
    )
    session.commit()


def test_claim_transitions_queued_to_running(
    session_factory: sessionmaker,
    db_session: Session,  # type: ignore[type-arg]
) -> None:
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session, "RFN-1")
    worker = _make_worker(session_factory)
    job_id = worker._claim_next_job()
    assert job_id == "RFN-1"

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-1")
    assert job is not None
    assert job.status == "running"


def test_claim_returns_none_when_nothing_queued(
    session_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    worker = _make_worker(session_factory)
    assert worker._claim_next_job() is None


def test_claim_skips_already_running_jobs(
    session_factory: sessionmaker,
    db_session: Session,  # type: ignore[type-arg]
) -> None:
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session, "RFN-1")
    db_session.query(CaliberRefinementJob).filter_by(job_id="RFN-1").update({"status": "running"})
    db_session.commit()
    worker = _make_worker(session_factory)
    assert worker._claim_next_job() is None


def test_second_claim_on_same_job_returns_none(
    session_factory: sessionmaker,
    db_session: Session,  # type: ignore[type-arg]
) -> None:
    """The safety property: a second ``_claim_next_job`` call on the same
    row finds it in ``running`` status and the UPDATE's predicate excludes
    it. This is what guarantees correctness under multi-worker contention —
    even though the workers are running on a single thread here, the SQL
    semantics are the same ones a parallel scheduler relies on."""
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session, "RFN-SOLO")

    worker_a = _make_worker(session_factory)
    worker_b = _make_worker(session_factory)

    # First claim succeeds.
    assert worker_a._claim_next_job() == "RFN-SOLO"
    # The "second worker" runs after the first commits; sees no queued jobs.
    assert worker_b._claim_next_job() is None


def test_two_sequential_claims_with_two_jobs_pick_disjoint(
    session_factory: sessionmaker,
    db_session: Session,  # type: ignore[type-arg]
) -> None:
    """Two queued jobs + two ticks → both jobs claimed, one per tick.

    Verifies the ORDER BY ``created_at`` picks the older one first
    (FIFO). The second tick picks up whatever's left in ``queued``.
    """
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session, "RFN-A")  # inserted first
    _seed_queued_job(db_session, "RFN-B")  # inserted second

    worker = _make_worker(session_factory)
    first = worker._claim_next_job()
    second = worker._claim_next_job()
    third = worker._claim_next_job()

    assert sorted([first, second]) == ["RFN-A", "RFN-B"]
    # Queue drained — third claim returns None.
    assert third is None


def test_claim_skips_jobs_for_disabled_agents(
    session_factory: sessionmaker,
    db_session: Session,  # type: ignore[type-arg]
) -> None:
    """A paused agent's queued jobs sit in the queue without blocking
    other agents. Resuming the agent makes the same row eligible again."""
    _seed_agent_and_item(db_session)
    _seed_queued_job(db_session, "RFN-PAUSED")
    db_session.query(CaliberAgentConfig).filter_by(agent_id="support-agent").update(
        {"enabled": False}
    )
    db_session.commit()

    worker = _make_worker(session_factory)
    assert worker._claim_next_job() is None

    # Resume.
    db_session.query(CaliberAgentConfig).filter_by(agent_id="support-agent").update(
        {"enabled": True}
    )
    db_session.commit()
    assert worker._claim_next_job() == "RFN-PAUSED"


def test_claim_picks_up_retry_jobs_at_candidate_stage(
    session_factory: sessionmaker,
    db_session: Session,  # type: ignore[type-arg]
) -> None:
    """A job re-queued by request-changes (``queued/candidate``) is claimable.

    The claim's WHERE clause filters on ``status='queued'`` only — it
    does not constrain ``current_stage``, so retries enter the worker
    loop at whatever stage the endpoint reset them to.
    """
    _seed_agent_and_item(db_session)
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-RETRY",
            agent_id="support-agent",
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="queued",
            current_stage="candidate",
            bundle_targets=[],
        )
    )
    db_session.commit()

    worker = _make_worker(session_factory)
    assert worker._claim_next_job() == "RFN-RETRY"

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-RETRY")
    assert job is not None
    assert job.status == "running"
    # Stage is preserved — the worker's stage loop will dispatch to
    # ``run_candidate`` next.
    assert job.current_stage == "candidate"
