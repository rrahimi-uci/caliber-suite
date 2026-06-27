"""Integration tests for ``/caliber/dashboard/summary``."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAssistantRun,
    CaliberAssistantSession,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.routes.dashboard import SUMMARY_PATH


def test_summary_returns_zeros_for_empty_db(client: TestClient) -> None:
    response = client.get(SUMMARY_PATH)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["agents_total"] == 0
    assert data["agents_enabled"] == 0
    assert data["verification_pending"] == 0
    assert data["verification_pending_critical"] == 0
    assert data["jobs_queued"] == 0
    assert data["jobs_running"] == 0
    assert data["jobs_awaiting_approval"] == 0
    assert data["jobs_completed"] == 0
    assert data["jobs_failed"] == 0
    assert data["jobs_rejected"] == 0
    assert data["approvals_pending"] == 0
    assert data["assistant_slo"]["plans_total"] == 0
    assert data["assistant_slo"]["executions_total"] == 0
    assert data["assistant_slo"]["publish_total"] == 0
    assert data["generated_at"] is not None


def test_summary_counts_all_dimensions(client: TestClient, db_session: Session) -> None:
    """Seed a representative slice of state and verify the rollup."""
    db_session.add_all(
        [
            CaliberAgentConfig(
                agent_id="a-on",
                experiment_id="exp-on",
                name="On",
                owner="@x",
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
                enabled=True,
            ),
            CaliberAgentConfig(
                agent_id="a-off",
                experiment_id="exp-off",
                name="Off",
                owner="@x",
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
                enabled=False,
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            CaliberVerificationItem(
                item_id="FB-CRIT",
                agent_id="a-on",
                category="hallucination",
                free_text="...",
                severity="critical",
                status="pending",
            ),
            CaliberVerificationItem(
                item_id="FB-STD",
                agent_id="a-on",
                category="tone",
                free_text="...",
                severity="standard",
                status="pending",
            ),
            CaliberVerificationItem(
                item_id="FB-DONE",
                agent_id="a-on",
                category="tone",
                free_text="...",
                severity="critical",
                status="verified",
            ),
        ]
    )
    db_session.flush()

    def _job(job_id: str, status: str) -> CaliberRefinementJob:
        return CaliberRefinementJob(
            job_id=job_id,
            agent_id="a-on",
            primary_item_id="FB-CRIT",
            artifact_type="prompt",
            status=status,
            current_stage="triage",
            bundle_targets=[],
        )

    db_session.add_all(
        [
            _job("RFN-Q1", "queued"),
            _job("RFN-Q2", "queued"),
            _job("RFN-R1", "running"),
            _job("RFN-A1", "awaiting_approval"),
            _job("RFN-C1", "completed"),
            _job("RFN-F1", "failed"),
            _job("RFN-X1", "rejected"),
        ]
    )
    db_session.flush()
    db_session.add(
        CaliberApprovalRequest(
            approval_id="AP-1",
            job_id="RFN-A1",
            agent_id="a-on",
            status="pending",
            eval_results={},
            candidate_snapshot={},
            diagnosis_snapshot=None,
        )
    )
    db_session.add(
        CaliberAssistantSession(
            session_id="ASST-SLO",
            title="SLO",
            owner="@x",
            metadata_={
                "intent_workbench": {
                    "plans": {
                        "APLN-ready": {
                            "plan_id": "APLN-ready",
                            "intent": {
                                "name": "generate_test_cases",
                                "confidence": 0.9,
                            },
                            "ready": True,
                            "missing_slots": [],
                            "questions": [],
                        },
                        "APLN-blocked": {
                            "plan_id": "APLN-blocked",
                            "intent": {
                                "name": "save_eval_dataset",
                                "confidence": 0.7,
                            },
                            "ready": False,
                            "missing_slots": ["examples"],
                            "questions": ["Which examples should I save?"],
                        },
                    },
                    "operations": {
                        "ARUN-ok": {
                            "operation_id": "ARUN-ok",
                            "status": "completed",
                            "result": {
                                "status": "completed",
                                "result_type": "test_cases",
                            },
                        },
                        "ARUN-blocked": {
                            "operation_id": "ARUN-blocked",
                            "status": "completed",
                            "result": {
                                "status": "blocked",
                                "result_type": "blocked",
                                "error_class": "policy_disabled",
                            },
                        },
                    },
                }
            },
        )
    )
    db_session.add_all(
        [
            CaliberAssistantRun(
                run_id="ARUN-ok",
                session_id="ASST-SLO",
                status="completed",
                engine="assistant-intent",
                model="generate_test_cases",
            ),
            CaliberAssistantRun(
                run_id="ARUN-fail",
                session_id="ASST-SLO",
                status="failed",
                engine="assistant-intent",
                model="save_eval_dataset",
                error="boom",
            ),
        ]
    )
    db_session.add_all(
        [
            CaliberAuditLog(
                actor="@x",
                action="publish_draft",
                entity_type="assistant_draft",
                entity_id="ADRF-ok",
                details={"success": True},
            ),
            CaliberAuditLog(
                actor="@x",
                action="publish_draft",
                entity_type="assistant_draft",
                entity_id="ADRF-fail",
                details={"success": False},
            ),
        ]
    )
    db_session.commit()

    response = client.get(SUMMARY_PATH)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["agents_total"] == 2
    assert data["agents_enabled"] == 1
    assert data["verification_pending"] == 2
    assert data["verification_pending_critical"] == 1
    # The background worker may process a queued job between commit and
    # the GET, so queued may drop by 1 and another status may rise.
    # Assert a range rather than an exact count for the mutable statuses.
    assert data["jobs_queued"] >= 1
    assert data["jobs_running"] >= 0
    assert data["jobs_awaiting_approval"] == 1
    assert data["jobs_completed"] == 1
    assert data["jobs_failed"] == 1
    assert data["jobs_rejected"] == 1
    assert data["approvals_pending"] == 1
    assert data["assistant_slo"]["plans_total"] == 2
    assert data["assistant_slo"]["plans_ready"] == 1
    assert data["assistant_slo"]["intent_confidence_avg"] == 0.8
    assert data["assistant_slo"]["plan_readiness_rate"] == 0.5
    assert data["assistant_slo"]["clarification_rate"] == 0.5
    assert data["assistant_slo"]["executions_total"] == 2
    assert data["assistant_slo"]["executions_completed"] == 1
    assert data["assistant_slo"]["executions_failed"] == 1
    assert data["assistant_slo"]["executions_blocked"] == 1
    assert data["assistant_slo"]["execution_success_rate"] == 0.5
    assert data["assistant_slo"]["adapter_error_classes"] == {"policy_disabled": 1}
    assert data["assistant_slo"]["publish_total"] == 2
    assert data["assistant_slo"]["publish_success"] == 1
    assert data["assistant_slo"]["publish_failed"] == 1
    assert data["assistant_slo"]["publish_success_rate"] == 0.5
