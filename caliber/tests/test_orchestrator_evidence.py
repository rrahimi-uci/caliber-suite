"""Tests for the evidence stage of the refinement pipeline."""

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
from caliber.orchestrator.evidence import EvidenceStateError, run_evidence


def _seed_job(
    session: Session,
    *,
    status: str = "running",
    stage: str = "evidence",
    trace_id: str | None = "tr-1",
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
        item_id="FB-EVD",
        agent_id="support-agent",
        category="hallucination",
        free_text="A long enough rationale for the evidence stub.",
        severity="critical",
        status="verified",
        trace_id=trace_id,
        experiment_id="exp",
    )
    session.add(item)
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-EVD",
        agent_id="support-agent",
        primary_item_id="FB-EVD",
        artifact_type="prompt",
        status=status,
        current_stage=stage,
        bundle_targets=[],
    )
    session.add(job)
    session.commit()
    return job


def test_run_evidence_advances_to_diagnosis(db_session: Session) -> None:
    _seed_job(db_session)
    job = run_evidence(db_session, "RFN-EVD")

    assert job.status == "running"
    assert job.current_stage == "diagnosis"


def test_run_evidence_writes_audit_row_with_evidence_summary(db_session: Session) -> None:
    _seed_job(db_session)
    run_evidence(db_session, "RFN-EVD", actor="@reza")

    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-EVD"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    log = rows[0]
    assert log.actor == "@reza"
    assert log.action == "advance_stage"
    assert log.details is not None
    assert log.details["from_stage"] == "evidence"
    assert log.details["to_stage"] == "diagnosis"
    summary = log.details["evidence"]
    assert summary["has_trace_link"] is True
    assert summary["trace_id"] == "tr-1"


def test_run_evidence_records_no_trace_link_when_absent(db_session: Session) -> None:
    _seed_job(db_session, trace_id=None)
    run_evidence(db_session, "RFN-EVD")
    log = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-EVD")
    ).scalar_one()
    assert log.details is not None
    assert log.details["evidence"]["has_trace_link"] is False


def test_run_evidence_rejects_unknown_job(db_session: Session) -> None:
    with pytest.raises(LookupError):
        run_evidence(db_session, "RFN-DOES-NOT-EXIST")


def test_run_evidence_rejects_queued_job(db_session: Session) -> None:
    """A queued job must go through triage first; evidence is for running jobs."""
    _seed_job(db_session, status="queued", stage="triage")
    with pytest.raises(EvidenceStateError):
        run_evidence(db_session, "RFN-EVD")


def test_run_evidence_rejects_already_past_evidence(db_session: Session) -> None:
    _seed_job(db_session, status="running", stage="diagnosis")
    with pytest.raises(EvidenceStateError):
        run_evidence(db_session, "RFN-EVD")
