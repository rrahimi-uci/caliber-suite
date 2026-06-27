"""Tests for the triage stage of the refinement pipeline."""

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
from caliber.orchestrator.triage import TriageStateError, run_triage


def _seed_job(
    session: Session,
    *,
    category: str = "hallucination",
    artifact_type: str = "",
    status: str = "running",
    stage: str = "triage",
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
        item_id="FB-X",
        agent_id="support-agent",
        category=category,
        free_text="...",
        severity="critical",
        status="verified",
    )
    session.add(item)
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-X",
        agent_id="support-agent",
        primary_item_id="FB-X",
        artifact_type=artifact_type,
        status=status,
        current_stage=stage,
        bundle_targets=[],
    )
    session.add(job)
    session.commit()
    return job


def test_run_triage_advances_to_evidence(db_session: Session) -> None:
    _seed_job(db_session)
    job = run_triage(db_session, "RFN-X", actor="system")

    assert job.status == "running"
    assert job.current_stage == "evidence"
    assert job.attempt_count == 1


def test_run_triage_backfills_artifact_type_for_hallucination(db_session: Session) -> None:
    """When the verifier didn't pin an artifact type, triage picks one."""
    _seed_job(db_session, category="hallucination", artifact_type="")
    job = run_triage(db_session, "RFN-X")
    assert job.artifact_type == "prompt"


def test_run_triage_classifies_context_drift_as_prompt(
    db_session: Session,
) -> None:
    _seed_job(db_session, category="context_drift", artifact_type="")
    job = run_triage(db_session, "RFN-X")
    assert job.artifact_type == "prompt"


def test_run_triage_preserves_existing_artifact_type(db_session: Session) -> None:
    """If the verifier already pinned an artifact type, triage respects it."""
    _seed_job(db_session, category="hallucination", artifact_type="guardrail")
    job = run_triage(db_session, "RFN-X")
    assert job.artifact_type == "guardrail"


def test_run_triage_writes_audit_row(db_session: Session) -> None:
    _seed_job(db_session)
    run_triage(db_session, "RFN-X", actor="@reza")
    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-X"))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    log = rows[0]
    assert log.actor == "@reza"
    assert log.action == "advance_stage"
    assert log.details is not None
    assert log.details["from_stage"] == "triage"
    assert log.details["to_stage"] == "evidence"


def test_run_triage_rejects_unknown_job(db_session: Session) -> None:
    with pytest.raises(LookupError, match=r"not found"):
        run_triage(db_session, "RFN-DOES-NOT-EXIST")


def test_run_triage_rejects_already_running(db_session: Session) -> None:
    _seed_job(db_session, status="running", stage="evidence")
    with pytest.raises(TriageStateError, match=r"not eligible"):
        run_triage(db_session, "RFN-X")


def test_run_triage_rejects_wrong_stage(db_session: Session) -> None:
    _seed_job(db_session, status="queued", stage="evidence")
    with pytest.raises(TriageStateError):
        run_triage(db_session, "RFN-X")


def test_run_triage_rejects_unclaimed_queued_job(db_session: Session) -> None:
    """Phase 3.1+: triage refuses to run on a job that hasn't been claimed yet.

    The worker's atomic claim is what transitions ``queued → running``;
    calling triage on a ``queued`` job directly bypasses that transaction
    and would race with other workers. The state guard refuses it.
    """
    _seed_job(db_session, status="queued", stage="triage")
    with pytest.raises(TriageStateError):
        run_triage(db_session, "RFN-X")
