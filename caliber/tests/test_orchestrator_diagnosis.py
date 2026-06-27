"""Tests for the diagnosis stage of the refinement pipeline.

These exercise the state-machine, the audit-log shape, the persistence of
the structured diagnosis to ``caliber_refinement_jobs.diagnosis``, and the
cost-tracking side effects — all without making a real LLM call. The
:class:`FakeLLMProvider` returns a deterministic Diagnosis + usage.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import Diagnosis, LLMProviderError, LLMUsage
from caliber.orchestrator.diagnosis import DiagnosisStateError, run_diagnosis


def _seed_job(
    session: Session,
    *,
    status: str = "running",
    stage: str = "diagnosis",
    initial_tokens: int = 0,
    initial_cost: float = 0.0,
) -> CaliberRefinementJob:
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
    item = CaliberVerificationItem(
        item_id="FB-DIAG",
        agent_id="support-agent",
        category="hallucination",
        free_text="Agent invented refund timeline.",
        severity="critical",
        status="verified",
        trace_id="tr-1",
        experiment_id="exp",
    )
    session.add(item)
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-DIAG",
        agent_id="support-agent",
        primary_item_id="FB-DIAG",
        artifact_type="prompt",
        status=status,
        current_stage=stage,
        bundle_targets=[],
        total_tokens=initial_tokens,
        cost_usd=initial_cost,
    )
    session.add(job)
    session.commit()
    return job


def test_run_diagnosis_advances_to_candidate(db_session: Session) -> None:
    _seed_job(db_session)
    job = run_diagnosis(db_session, "RFN-DIAG", FakeLLMProvider())

    assert job.status == "running"
    assert job.current_stage == "candidate"


def test_run_diagnosis_persists_structured_output(db_session: Session) -> None:
    """The job row gains a JSON ``diagnosis`` payload with the canonical shape."""
    _seed_job(db_session)
    canned = Diagnosis(
        root_cause="Prompt does not require lookup_policy",
        affected_components=["prompt", "guardrail"],
        confidence=0.87,
        alternatives=["Stale tool description"],
    )
    provider = FakeLLMProvider(diagnose_response=canned)
    run_diagnosis(db_session, "RFN-DIAG", provider)

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-DIAG")
    assert job is not None
    assert job.diagnosis == {
        "root_cause": "Prompt does not require lookup_policy",
        "affected_components": ["prompt", "guardrail"],
        "confidence": 0.87,
        "alternatives": ["Stale tool description"],
    }


def test_run_diagnosis_accumulates_token_and_cost(db_session: Session) -> None:
    _seed_job(db_session, initial_tokens=100, initial_cost=0.005)
    provider = FakeLLMProvider(
        diagnose_usage=LLMUsage(input_tokens=300, output_tokens=80, cost_usd=0.002)
    )
    run_diagnosis(db_session, "RFN-DIAG", provider)

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-DIAG")
    assert job is not None
    assert job.total_tokens == 100 + 300 + 80
    assert job.cost_usd == pytest.approx(0.005 + 0.002)


def test_run_diagnosis_passes_evidence_to_provider(db_session: Session) -> None:
    """The provider sees the verification-item content as evidence context."""
    _seed_job(db_session)
    provider = FakeLLMProvider()
    run_diagnosis(db_session, "RFN-DIAG", provider)

    assert len(provider.calls) == 1
    evidence = provider.calls[0]
    assert evidence.agent_id == "support-agent"
    assert evidence.item_id == "FB-DIAG"
    assert evidence.category == "hallucination"
    assert evidence.severity == "critical"
    assert evidence.trace_id == "tr-1"


def test_run_diagnosis_writes_audit_row(db_session: Session) -> None:
    _seed_job(db_session)
    run_diagnosis(db_session, "RFN-DIAG", FakeLLMProvider(), actor="@reza")

    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-DIAG"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    log = rows[0]
    assert log.actor == "@reza"
    assert log.action == "advance_stage"
    assert log.details is not None
    assert log.details["from_stage"] == "diagnosis"
    assert log.details["to_stage"] == "candidate"
    assert "diagnosis" in log.details
    assert "usage" in log.details


def test_run_diagnosis_rejects_unknown_job(db_session: Session) -> None:
    with pytest.raises(LookupError):
        run_diagnosis(db_session, "RFN-NONE", FakeLLMProvider())


def test_run_diagnosis_rejects_queued_job(db_session: Session) -> None:
    """A queued job must go through triage and evidence first."""
    _seed_job(db_session, status="queued", stage="triage")
    with pytest.raises(DiagnosisStateError):
        run_diagnosis(db_session, "RFN-DIAG", FakeLLMProvider())


def test_run_diagnosis_rejects_already_past_diagnosis(db_session: Session) -> None:
    _seed_job(db_session, status="running", stage="candidate")
    with pytest.raises(DiagnosisStateError):
        run_diagnosis(db_session, "RFN-DIAG", FakeLLMProvider())


def test_run_diagnosis_propagates_llm_provider_error(db_session: Session) -> None:
    """Provider failures roll the transaction back and surface to the worker."""
    _seed_job(db_session)

    def boom(_evidence: object) -> tuple[Diagnosis, LLMUsage]:
        raise LLMProviderError("simulated rate limit")

    provider = FakeLLMProvider(diagnose_callable=boom)
    with pytest.raises(LLMProviderError, match=r"rate limit"):
        run_diagnosis(db_session, "RFN-DIAG", provider)

    # State machine untouched.
    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-DIAG")
    assert job is not None
    assert job.current_stage == "diagnosis"
    assert job.diagnosis is None
