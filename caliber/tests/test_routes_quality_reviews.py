"""Integration tests for ``/caliber/jobs/{job_id}/quality-reviews``.

A human go/no-go decision on a refinement job's candidate, distinct from the
machine eval gate. ``"go"`` is advisory (the job is untouched); ``"no_go"``
terminally rejects the job and creates a ``CaliberReworkTask`` with
``failure_kind="quality_no_go"``, the same way ``orchestrator/eval_stage.py``
does for a machine-gate rejection.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberQualityReview,
    CaliberRefinementJob,
    CaliberReworkTask,
    CaliberVerificationItem,
)
from caliber.routes.quality_reviews import PATH


def _seed_candidate_ready_job(session: Session, job_id: str = "RFN-QR") -> None:
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
            item_id="FB-QR",
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
            primary_item_id="FB-QR",
            artifact_type="prompt",
            status="candidate_ready",
            current_stage="done",
            bundle_targets=[],
            candidate={"content": "rewritten prompt body", "artifact_type": "prompt"},
            eval_results={"gate": {"passed": True}, "overall": 0.94},
        )
    )
    session.commit()


def _url(job_id: str) -> str:
    return PATH.replace("{job_id}", job_id)


def test_go_review_records_row_and_leaves_job_untouched(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        _url("RFN-QR"),
        json={"decision": "go", "rationale": "cites the refund policy correctly"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["job_id"] == "RFN-QR"
    assert data["agent_id"] == "support-agent"
    assert data["decision"] == "go"
    assert data["rationale"] == "cites the refund policy correctly"
    assert data["decided_by"] == "@test"
    assert data["candidate_snapshot"]["content"] == "rewritten prompt body"
    assert data["eval_results_snapshot"]["overall"] == 0.94
    assert data["created_at"] is not None

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-QR")
    assert job is not None
    assert job.status == "candidate_ready"
    assert job.error_message is None

    assert db_session.query(CaliberReworkTask).count() == 0


def test_no_go_review_rejects_job_and_creates_rework_task(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        _url("RFN-QR"),
        json={"decision": "no_go", "rationale": "misses a required disclaimer"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["decision"] == "no_go"

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-QR")
    assert job is not None
    assert job.status == "rejected"
    assert job.current_stage == "done"
    assert job.error_message == "quality review: misses a required disclaimer"

    task = db_session.query(CaliberReworkTask).one()
    assert task.job_id == "RFN-QR"
    assert task.agent_id == "support-agent"
    assert task.failure_kind == "quality_no_go"
    assert task.reason == "misses a required disclaimer"
    assert task.gate_evidence is None
    assert task.status == "open"

    actions = {
        row.action
        for row in db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.entity_id.in_(["RFN-QR", task.task_id]))
        .all()
    }
    assert actions == {"submit_quality_review", "create_rework_task"}


def test_no_go_on_non_candidate_ready_job_returns_409(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-QR")
    assert job is not None
    job.status = "running"
    db_session.commit()

    response = client.post(
        _url("RFN-QR"),
        json={"decision": "no_go", "rationale": "misses a required disclaimer"},
    )
    assert response.status_code == 409
    assert db_session.query(CaliberQualityReview).count() == 0
    assert db_session.query(CaliberReworkTask).count() == 0


def test_go_on_non_candidate_ready_job_returns_409(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-QR")
    assert job is not None
    job.status = "applied"
    db_session.commit()

    response = client.post(
        _url("RFN-QR"),
        json={"decision": "go", "rationale": "cites the refund policy correctly"},
    )
    assert response.status_code == 409


def test_review_missing_job_returns_404(client: TestClient) -> None:
    response = client.post(
        _url("RFN-GHOST"),
        json={"decision": "go", "rationale": "cites the refund policy correctly"},
    )
    assert response.status_code == 404


def test_review_invalid_decision_returns_400(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        _url("RFN-QR"),
        json={"decision": "maybe", "rationale": "cites the refund policy correctly"},
    )
    assert response.status_code == 400


def test_review_rationale_too_short_returns_400(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(_url("RFN-QR"), json={"decision": "go", "rationale": "ok"})
    assert response.status_code == 400


def test_review_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        _url("RFN-QR"),
        json={"decision": "go", "rationale": "cites the refund policy correctly"},
        headers={"X-CALIBER-User": ""},
    )
    assert response.status_code in (401, 403)


def test_multiple_go_reviews_are_all_recorded(client: TestClient, db_session: Session) -> None:
    """Append-only: reviewing "go" twice keeps both rows, doesn't upsert."""
    _seed_candidate_ready_job(db_session)
    client.post(_url("RFN-QR"), json={"decision": "go", "rationale": "looks solid to me"})
    client.post(_url("RFN-QR"), json={"decision": "go", "rationale": "confirmed independently"})

    rows = db_session.execute(select(CaliberQualityReview)).scalars().all()
    assert len(rows) == 2


def test_list_reviews_returns_newest_first(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    client.post(_url("RFN-QR"), json={"decision": "go", "rationale": "first pass looks fine"})
    client.post(
        _url("RFN-QR"), json={"decision": "no_go", "rationale": "second pass found an issue"}
    )

    response = client.get(_url("RFN-QR"))
    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 2
    assert rows[0]["decision"] == "no_go"
    assert rows[1]["decision"] == "go"


def test_list_reviews_empty_for_a_job_with_none(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.get(_url("RFN-QR"))
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_list_reviews_missing_job_returns_404(client: TestClient) -> None:
    response = client.get(_url("RFN-GHOST"))
    assert response.status_code == 404
