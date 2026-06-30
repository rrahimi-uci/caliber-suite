"""Integration tests for ``POST /caliber/jobs/{job_id}/apply``.

The lightweight operator Apply flow replaces the removed approval-governance
endpoints: a job that cleared the eval gate (``candidate_ready``) is promoted
directly, no votes/quorum/comments. A born-``approved`` ``CaliberApprovalRequest``
is minted as the rollback/provenance anchor.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberVerificationItem,
)

APPLY_PATH = "/ajax-api/2.0/mlflow/caliber/jobs/{job_id}/apply"


def _seed_candidate_ready_job(
    session: Session,
    *,
    job_id: str = "RFN-A",
    status: str = "candidate_ready",
    candidate: dict[str, object] | None = None,
    artifact_type: str = "prompt",
) -> None:
    if session.get(CaliberAgentConfig, "support-agent") is None:
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
                item_id="FB-A",
                agent_id="support-agent",
                category="hallucination",
                free_text="...",
                severity="critical",
                status="verified",
            )
        )
        session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            workflow_id="WF-1",
            primary_item_id="FB-A",
            artifact_type=artifact_type,
            status=status,
            current_stage="done" if status == "candidate_ready" else "candidate",
            attempt_count=1,
            bundle_targets=[],
            diagnosis={"root_cause": "x", "confidence": 0.9, "alternatives": []},
            eval_results={"gate": {"passed": True}},
            candidate=candidate
            or {
                "artifact_type": "prompt",
                "content": "rewritten prompt body",
                "rationale": "applies tool-use directive",
                "diff_summary": "+5 / -3",
            },
        )
    )
    session.commit()


def test_apply_promotes_candidate_and_marks_applied(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)

    response = client.post(
        APPLY_PATH.format(job_id="RFN-A"),
        headers={"X-CALIBER-User": "@reza"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["job_id"] == "RFN-A"
    assert data["status"] == "applied"
    assert "artifact_ref" in data["promotion"]

    # The candidate content was handed to the (fake) promoter.
    promoter = client.app.state.promoter  # type: ignore[attr-defined]
    assert len(promoter.calls) == 1
    req = promoter.calls[0]
    assert req.agent_id == "support-agent"
    assert req.artifact_type == "prompt"
    assert req.new_content == "rewritten prompt body"

    # The job is terminal at ``applied``.
    job = db_session.get(CaliberRefinementJob, "RFN-A")
    assert job is not None
    assert job.status == "applied"
    assert job.current_stage == "done"


def test_apply_mints_born_approved_anchor_and_checkpoint(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    client.post(APPLY_PATH.format(job_id="RFN-A"), headers={"X-CALIBER-User": "@reza"})

    approval = db_session.query(CaliberApprovalRequest).one()
    assert approval.job_id == "RFN-A"
    assert approval.status == "approved"
    assert approval.approved_by == "@reza"
    assert approval.approved_at is not None
    assert approval.candidate_snapshot is not None
    assert approval.candidate_snapshot["content"] == "rewritten prompt body"

    # A rollback checkpoint was anchored to the born-approved request.
    checkpoint = db_session.query(CaliberRollbackCheckpoint).one()
    assert checkpoint.approval_id == approval.approval_id
    assert checkpoint.agent_id == "support-agent"


def test_apply_writes_apply_and_promote_audit_rows(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    client.post(APPLY_PATH.format(job_id="RFN-A"))

    actions = {
        row.action
        for row in db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.entity_id == "RFN-A")
        .all()
    }
    assert "apply_candidate" in actions
    assert "promote" in actions


def test_apply_on_non_candidate_ready_job_returns_409(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session, status="running")
    response = client.post(APPLY_PATH.format(job_id="RFN-A"))
    assert response.status_code == 409


def test_apply_missing_job_returns_404(client: TestClient) -> None:
    response = client.post(APPLY_PATH.format(job_id="RFN-NONE"))
    assert response.status_code == 404


def test_apply_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        APPLY_PATH.format(job_id="RFN-A"),
        headers={"X-CALIBER-User": ""},
    )
    assert response.status_code in (401, 403)


def test_apply_prompt_alias_proposal_rotates_existing_version(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_candidate_ready_job(
        db_session,
        candidate={
            "artifact_type": "prompt",
            "promotion_type": "prompt_alias",
            "source_version": 7,
            "target_alias": "prod",
            "content": "rewritten prompt body",
        },
    )

    captured: dict[str, object] = {}

    def fake_set_prompt_alias_version(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"name": "support-agent", "alias": "prod", "version": 7}

    monkeypatch.setattr(
        "caliber.routes.prompts.set_prompt_alias_version",
        fake_set_prompt_alias_version,
    )

    response = client.post(APPLY_PATH.format(job_id="RFN-A"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "applied"
    assert data["promotion"]["artifact_ref"] == "prompts:/support-agent@prod"
    assert captured == {"name": "support-agent", "alias": "prod", "version": 7}
    # Prompt-alias rotation goes through the prompts route, not the bundle promoter.
    assert client.app.state.promoter.calls == []  # type: ignore[attr-defined]

    checkpoint = db_session.query(CaliberRollbackCheckpoint).one()
    assert checkpoint.snapshot_payload is not None
    assert checkpoint.snapshot_payload["promotion_type"] == "prompt_alias"
