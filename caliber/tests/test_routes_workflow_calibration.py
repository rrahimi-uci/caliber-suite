"""Workflow calibration route tests."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
    seed_eval_dataset,
)


def _seed_agent(session: Session, *, enabled: bool = True) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp-support-calibration",
            name="Support Agent",
            owner="@test",
            enabled=enabled,
        )
    )
    session.commit()


def _manifest(workflow_id: str) -> dict[str, object]:
    return make_support_manifest(
        workflow_id,
        deploy_gates={
            "support_eval_gate": {
                "type": "deploy_gate",
                "dataset_ref": "support_eval",
                "required_for_aliases": ["prod"],
                "thresholds": {"min_pass_rate": 1.0},
            }
        },
    )


def _workflow_with_dataset(client: TestClient, db_session: Session) -> tuple[str, str]:
    _seed_agent(db_session)
    seed_eval_dataset(db_session)
    register_demo_tools(client)
    workflow_id = create_workflow(client, "Calibratable Workflow")
    version_id, _hash = create_draft(client, workflow_id, _manifest(workflow_id))
    publish = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert publish.status_code == 200, publish.text
    return workflow_id, version_id


def test_workflow_calibration_options_include_dataset_availability(
    client: TestClient,
    db_session: Session,
) -> None:
    workflow_id, version_id = _workflow_with_dataset(client, db_session)

    resp = client.get(f"{PREFIX}/workflows/{workflow_id}/calibration/options")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "quality" in data["supported_objectives"]
    assert "add_grounding_guardrail" in data["supported_move_set"]
    assert data["default_budget"]["max_candidates"] == 3
    assert data["data"]["workflow_version_id"] == version_id
    assert data["data"]["deploy_gate_dataset"]["available"] is True
    assert data["data"]["deploy_gate_dataset"]["example_count"] >= 1
    assert data["data"]["judge"]["available"] is False
    assert "workflow_llm_judge_enabled" in data["data"]["judge"]["reason"]


def test_workflow_calibration_options_report_judge_availability_when_configured(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    workflow_id, _version_id = _workflow_with_dataset(client, db_session)
    monkeypatch.setenv("JUDGE_TEST_KEY", "sk-test")
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_llm_judge_enabled": True,
            "llm_provider": "openai",
            "llm_api_key_env": "JUDGE_TEST_KEY",
            "llm_diagnosis_model": "gpt-4o-mini",
        }
    )

    resp = client.get(f"{PREFIX}/workflows/{workflow_id}/calibration/options")

    assert resp.status_code == 200
    judge = resp.json()["data"]["data"]["judge"]
    assert judge == {
        "available": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
    }


def test_workflow_calibration_run_creates_verified_item_and_queued_job(
    client: TestClient,
    db_session: Session,
) -> None:
    workflow_id, version_id = _workflow_with_dataset(client, db_session)

    resp = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={
            "agent_id": "support-agent",
            "objective": {"maximize": "quality", "epsilon": 0.02},
            "budget": {"max_candidates": 3, "max_eval_examples": 20, "min_examples": 2},
        },
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    job = data["job"]
    item = data["item"]
    assert item["category"] == "workflow_calibration"
    assert item["status"] == "verified"
    assert job["artifact_type"] == "workflow_manifest"
    assert job["current_stage"] == "diagnosis"
    assert job["status"] == "queued"
    assert job["calibration_spec"]["workflow_id"] == workflow_id
    assert job["calibration_spec"]["workflow_version_id"] == version_id
    assert job["calibration_spec"]["dataset_summary"]["available"] is True

    db_session.expire_all()
    stored_job = db_session.get(CaliberRefinementJob, job["job_id"])
    stored_item = db_session.get(CaliberVerificationItem, item["item_id"])
    assert stored_job is not None
    assert stored_item is not None
    assert stored_job.calibration_spec["objective"]["maximize"] == "quality"
    assert stored_item.submitted_context["calibration_spec"]["workflow_id"] == workflow_id


def test_workflow_calibration_job_serialization_includes_spec(
    client: TestClient,
    db_session: Session,
) -> None:
    workflow_id, _version_id = _workflow_with_dataset(client, db_session)
    created = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={"agent_id": "support-agent"},
    ).json()["data"]["job"]

    detail = client.get(f"{PREFIX}/jobs/{created['job_id']}")

    assert detail.status_code == 200
    assert detail.json()["data"]["calibration_spec"]["workflow_id"] == workflow_id


def test_workflow_calibration_run_accepts_judge_when_configured(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    workflow_id, _version_id = _workflow_with_dataset(client, db_session)
    monkeypatch.setenv("JUDGE_TEST_KEY", "sk-test")
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_llm_judge_enabled": True,
            "llm_provider": "openai",
            "llm_api_key_env": "JUDGE_TEST_KEY",
            "llm_diagnosis_model": "gpt-4o-mini",
        }
    )

    resp = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={"agent_id": "support-agent", "judge": {"enabled": True}},
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["job"]["calibration_spec"]["judge"]["enabled"] is True

    db_session.expire_all()
    stored_job = db_session.get(CaliberRefinementJob, data["job"]["job_id"])
    assert stored_job is not None
    assert stored_job.calibration_spec["judge"]["enabled"] is True


def test_workflow_calibration_rejects_missing_dataset(
    client: TestClient,
    db_session: Session,
) -> None:
    _seed_agent(db_session)
    register_demo_tools(client)
    workflow_id = create_workflow(client, "No Dataset Workflow")
    version_id, _hash = create_draft(client, workflow_id, _manifest(workflow_id))
    publish = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert publish.status_code == 200, publish.text

    resp = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={"agent_id": "support-agent"},
    )

    assert resp.status_code == 400
    assert "eval dataset" in resp.json()["detail"]


def test_workflow_calibration_rejects_unknown_agent(
    client: TestClient,
    db_session: Session,
) -> None:
    seed_eval_dataset(db_session)
    register_demo_tools(client)
    workflow_id = create_workflow(client, "Unknown Agent Workflow")
    version_id, _hash = create_draft(client, workflow_id, _manifest(workflow_id))
    publish = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert publish.status_code == 200, publish.text

    resp = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={"agent_id": "missing-agent"},
    )

    assert resp.status_code == 400
    assert "missing-agent" in resp.json()["detail"]


def test_workflow_calibration_rejects_invalid_move_set(
    client: TestClient,
    db_session: Session,
) -> None:
    workflow_id, _version_id = _workflow_with_dataset(client, db_session)

    invalid_move = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={"agent_id": "support-agent", "move_set": ["agent_tool_add"]},
    )
    assert invalid_move.status_code == 400
    assert "unknown calibration move" in invalid_move.json()["detail"]


def test_workflow_calibration_rejects_judge_when_unavailable(
    client: TestClient,
    db_session: Session,
) -> None:
    workflow_id, _version_id = _workflow_with_dataset(client, db_session)

    judge = client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={"agent_id": "support-agent", "judge": {"enabled": True}},
    )
    assert judge.status_code == 400
    assert "LLM judge is unavailable" in judge.json()["detail"]
