"""Integration tests for ``/caliber/jobs``."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.routes.jobs import DETAIL_PATH, LIST_PATH


def _seed(session: Session) -> None:
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
    session.flush()
    session.add_all(
        [
            CaliberRefinementJob(
                job_id="RFN-A",
                agent_id="support-agent",
                workflow_id="WF-1",
                primary_item_id="FB-1",
                artifact_type="prompt",
                status="queued",
                current_stage="triage",
                bundle_targets=[],
            ),
            CaliberRefinementJob(
                job_id="RFN-B",
                agent_id="support-agent",
                workflow_id="WF-1",
                primary_item_id="FB-1",
                artifact_type="prompt",
                status="running",
                current_stage="evidence",
                bundle_targets=[],
            ),
            CaliberRefinementJob(
                job_id="RFN-C",
                agent_id="support-agent",
                workflow_id="WF-2",
                primary_item_id="FB-1",
                artifact_type="skill",
                skill_name="tool-use",
                status="completed",
                current_stage="done",
                bundle_targets=[],
            ),
        ]
    )
    session.commit()


def test_list_jobs_returns_all_unfiltered(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH)
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-A", "RFN-B", "RFN-C"}


def test_list_jobs_filter_by_status(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH, params={"status": "running"})
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-B"}


def test_list_jobs_filter_by_stage(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH, params={"stage": "triage"})
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-A"}


def test_list_jobs_filter_by_workflow_id(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH, params={"workflow_id": "WF-1"})
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-A", "RFN-B"}


def test_list_jobs_invalid_status_returns_400(client: TestClient) -> None:
    response = client.get(LIST_PATH, params={"status": "bogus"})
    assert response.status_code == 400


def test_list_jobs_invalid_stage_returns_400(client: TestClient) -> None:
    response = client.get(LIST_PATH, params={"stage": "bogus"})
    assert response.status_code == 400


def test_get_job_returns_record(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-A"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["job_id"] == "RFN-A"
    assert data["current_stage"] == "triage"
    assert data["status"] == "queued"


def test_get_job_includes_skill_name(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-C"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artifact_type"] == "skill"
    assert data["skill_name"] == "tool-use"


def test_get_job_404_when_missing(client: TestClient) -> None:
    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-NONE"))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /jobs/{id}/targets — bundle review
# ---------------------------------------------------------------------------


def _seed_bundle_job(
    session: Session,
    job_id: str,
    bundle_targets: list[dict[str, object]],
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
                item_id="FB-1",
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
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="running",
            current_stage="candidate",
            bundle_targets=bundle_targets,
        )
    )
    session.commit()


def test_get_job_targets_single_agent_returns_one_row(
    client: TestClient, db_session: Session
) -> None:
    """A job with no bundle_targets entries returns one row — its own
    agent/artifact_type — so the UI doesn't need a special case."""
    _seed_bundle_job(db_session, "RFN-SOLO", [])
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-SOLO/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bundle_size"] == 1
    assert len(data["targets"]) == 1
    assert data["targets"][0]["agent_id"] == "support-agent"
    assert data["targets"][0]["artifact_type"] == "prompt"


def test_get_job_targets_bundle_returns_one_row_per_agent(
    client: TestClient, db_session: Session
) -> None:
    bundle = [
        {"agent_id": "coordinator", "artifact_type": "coordinator_policy", "role": "lead"},
        {"agent_id": "router", "artifact_type": "routing_policy"},
        {"agent_id": "support-agent", "artifact_type": "role_prompt"},
    ]
    _seed_bundle_job(db_session, "RFN-BUNDLE", bundle)

    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-BUNDLE/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bundle_size"] == 3
    by_agent = {row["agent_id"]: row for row in data["targets"]}
    assert by_agent["coordinator"]["artifact_type"] == "coordinator_policy"
    assert by_agent["coordinator"]["role"] == "lead"
    assert by_agent["router"]["artifact_type"] == "routing_policy"
    assert by_agent["support-agent"]["artifact_type"] == "role_prompt"


def test_get_job_targets_preserves_extra_keys(client: TestClient, db_session: Session) -> None:
    """``bundle_targets`` entries can carry richer metadata; we shouldn't
    drop it (the UI may render blast-radius scores, etc.)."""
    bundle = [
        {
            "agent_id": "coordinator",
            "artifact_type": "coordinator_policy",
            "blast_radius": 0.78,
            "current_version": 5,
        }
    ]
    _seed_bundle_job(db_session, "RFN-RICH", bundle)
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-RICH/targets")
    assert response.status_code == 200
    target = response.json()["data"]["targets"][0]
    assert target["blast_radius"] == 0.78
    assert target["current_version"] == 5


def test_get_job_targets_404_when_missing(client: TestClient) -> None:
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-GHOST/targets")
    assert response.status_code == 404


def test_get_job_targets_ignores_non_dict_entries(client: TestClient, db_session: Session) -> None:
    """Defensive: a malformed bundle entry shouldn't crash the endpoint."""
    bundle: list[dict[str, object]] = [
        {"agent_id": "ok", "artifact_type": "prompt"},
        "not-a-dict",  # type: ignore[list-item]
    ]
    _seed_bundle_job(db_session, "RFN-BAD", bundle)
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-BAD/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["targets"]) == 1
    assert data["targets"][0]["agent_id"] == "ok"


def test_get_job_targets_handles_null_field_values(client: TestClient, db_session: Session) -> None:
    """``{"agent_id": None, ...}`` must fall back to the job's
    primary agent_id rather than serializing as ``"None"``
    (deep-review Finding 6)."""
    bundle: list[dict[str, object]] = [
        {"agent_id": None, "artifact_type": "prompt"},
        {"agent_id": "real-agent", "artifact_type": None},
    ]
    _seed_bundle_job(db_session, "RFN-NULLS", bundle)
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-NULLS/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["targets"][0]["agent_id"] == "support-agent"  # job's primary
    assert data["targets"][0]["artifact_type"] == "prompt"
    assert data["targets"][1]["agent_id"] == "real-agent"
    assert data["targets"][1]["artifact_type"] == "prompt"  # falls back to job's
    # No row carries the literal string ``"None"``.
    assert all(t["agent_id"] != "None" for t in data["targets"])
    assert all(t["artifact_type"] != "None" for t in data["targets"])
