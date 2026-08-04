"""End-to-end release rubric, waiver, signoff, and Allure-report coverage."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberReleaseCandidate,
    CaliberReleaseReportJob,
    CaliberReleaseSignoff,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber/releases"


def _candidate(*, score: float = 0.95, blocking_score: float = 1.0) -> dict[str, object]:
    return {
        "name": "Support workflow v7 release",
        "artifact_type": "workflow",
        "artifact_ref": "WF-support",
        "version_ref": "WFV-7",
        "required_score": 0.8,
        "criteria": [
            {
                "key": "quality",
                "title": "Evaluation pass rate",
                "weight": 70,
                "score": score,
                "threshold": 0.9,
                "blocking": True,
                "evidence_refs": ["EVR-123"],
            },
            {
                "key": "human_review",
                "title": "Human review completion",
                "weight": 30,
                "score": blocking_score,
                "threshold": 1,
                "blocking": True,
                "evidence_refs": ["RVQ-123"],
            },
        ],
        "evidence": [
            {
                "evidence_type": "evaluation_run",
                "evidence_ref": "EVR-123",
                "label": "Sealed regression evaluation",
                "digest": "a" * 64,
            },
            {
                "evidence_type": "review_queue",
                "evidence_ref": "RVQ-123",
                "label": "Completed expert review",
            },
        ],
        "planned_action": {"action": "promote_workflow", "alias": "prod"},
        "rollback_target": {"version_ref": "WFV-6", "alias": "prod"},
    }


def _create(client: TestClient, **scores: float) -> dict[str, object]:
    response = client.post(f"{PREFIX}/candidates", json=_candidate(**scores))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_ready_candidate_scores_signs_and_generates_allure_report(
    client: TestClient,
    db_session: Session,
) -> None:
    candidate = _create(client)
    assert candidate["status"] == "ready"
    assert candidate["weighted_score"] == 0.965
    assert candidate["blockers"] == []

    signed = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/signoffs",
        json={"decision": "go", "rationale": "All required release evidence is green."},
    )
    assert signed.status_code == 201, signed.text
    signoff = signed.json()["data"]
    assert signoff["decision"] == "go"
    assert signoff["candidate_snapshot"]["status"] == "ready"

    report_response = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/reports",
        json={},
    )
    assert report_response.status_code == 201, report_response.text
    job = report_response.json()["data"]
    assert job["status"] == "completed"
    assert job["format"] == "allure-json"
    assert job["report"]["format"] == "allure-compatible-results"
    assert len(job["report"]["candidate_snapshot_sha256"]) == 64
    assert [result["status"] for result in job["report"]["results"]] == [
        "passed",
        "passed",
    ]
    fetched = client.get(f"{PREFIX}/report-jobs/{job['report_job_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["report_job_id"] == job["report_job_id"]

    assert (
        db_session.execute(
            select(CaliberReleaseSignoff).where(
                CaliberReleaseSignoff.candidate_id == candidate["candidate_id"]
            )
        )
        .scalar_one()
        .decided_by
        == "@test"
    )
    assert (
        db_session.execute(
            select(CaliberReleaseReportJob).where(
                CaliberReleaseReportJob.candidate_id == candidate["candidate_id"]
            )
        )
        .scalar_one()
        .status
        == "completed"
    )


def test_blocker_requires_admin_waiver_before_go(client: TestClient) -> None:
    # The candidate clears the aggregate score but fails one blocking threshold;
    # a waiver may remove that blocker without rewriting the observed score.
    candidate = _create(client, blocking_score=0.6)
    assert candidate["status"] == "blocked"
    assert [blocker["criterion_key"] for blocker in candidate["blockers"]] == ["human_review"]
    denied = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/signoffs",
        json={"decision": "go", "rationale": "Trying to bypass the blocker."},
    )
    assert denied.status_code == 409

    waived = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/waivers",
        json={
            "criterion_key": "human_review",
            "reason": "Approved incident-only exception with compensating monitoring.",
        },
    )
    assert waived.status_code == 200, waived.text
    assert waived.json()["data"]["status"] == "ready"
    assert waived.json()["data"]["blockers"] == []

    signed = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/signoffs",
        json={"decision": "go", "rationale": "Exception is documented and monitored."},
    )
    assert signed.status_code == 201
    report = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/reports", json={}
    ).json()["data"]["report"]
    assert [result["status"] for result in report["results"]] == ["passed", "skipped"]
    assert report["results"][1]["statusDetails"]["message"] == "waived"


def test_no_go_is_accountable_and_final(client: TestClient, db_session: Session) -> None:
    candidate = _create(client, score=0.1)
    response = client.post(
        f"{PREFIX}/candidates/{candidate['candidate_id']}/signoffs",
        json={"decision": "no_go", "rationale": "Quality is below the declared threshold."},
    )
    assert response.status_code == 201
    detail = client.get(f"{PREFIX}/candidates/{candidate['candidate_id']}").json()["data"]
    assert detail["candidate"]["status"] == "signed_no_go"
    assert len(detail["signoffs"]) == 1
    assert (
        client.post(
            f"{PREFIX}/candidates/{candidate['candidate_id']}/signoffs",
            json={"decision": "go", "rationale": "A second final decision is forbidden."},
        ).status_code
        == 409
    )
    audit_actions = set(
        db_session.execute(
            select(CaliberAuditLog.action).where(
                CaliberAuditLog.entity_id == candidate["candidate_id"]
            )
        ).scalars()
    )
    assert {"create_release_candidate", "signoff_release_candidate"} <= audit_actions


def test_viewer_can_list_but_cannot_create_or_sign(client: TestClient) -> None:
    headers = {"X-CALIBER-User": "viewer-only"}
    assert client.get(f"{PREFIX}/candidates", headers=headers).status_code == 200
    assert (
        client.post(f"{PREFIX}/candidates", json=_candidate(), headers=headers).status_code == 403
    )


def test_candidate_row_is_durable(client: TestClient, db_session: Session) -> None:
    candidate = _create(client)
    stored = db_session.get(CaliberReleaseCandidate, candidate["candidate_id"])
    assert stored is not None
    assert stored.rollback_target == {"version_ref": "WFV-6", "alias": "prod"}
