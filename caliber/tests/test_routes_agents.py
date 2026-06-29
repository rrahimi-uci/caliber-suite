"""Integration tests for ``/caliber/agents``."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.routes.agents import DETAIL_PATH, LIST_PATH


def _insert_agent(session: Session, **overrides: object) -> CaliberAgentConfig:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "experiment_id": "exp-support-prod",
        "name": "Support Agent",
        "owner": "@sarah",
        "artifact_types": ["prompt"],
        "eval_thresholds": {},
        "optimizer_config": {},
        "approval_policy": {},
    }
    defaults.update(overrides)
    agent = CaliberAgentConfig(**defaults)
    session.add(agent)
    session.commit()
    return agent


def test_list_agents_empty(client: TestClient) -> None:
    response = client.get(LIST_PATH)
    assert response.status_code == 200
    assert response.json() == {"data": [], "next_cursor": None} or response.json()["data"] == []


def test_list_agents_returns_inserted_rows(client: TestClient, db_session: Session) -> None:
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1")
    _insert_agent(db_session, agent_id="a-2", experiment_id="exp-2")

    response = client.get(LIST_PATH)
    assert response.status_code == 200
    body = response.json()
    agent_ids = {item["agent_id"] for item in body["data"]}
    assert agent_ids == {"a-1", "a-2"}


def test_list_agents_excludes_hidden_prompt_targets(
    client: TestClient, db_session: Session
) -> None:
    """Auto-provisioned hidden prompt targets are a runtime detail — never agents."""
    _insert_agent(db_session, agent_id="real-agent", experiment_id="exp-real")
    # A hidden prompt-target row (source_type marker set on optimizer_config).
    _insert_agent(
        db_session,
        agent_id="my-prompt",
        experiment_id="prompt-target-abc123",
        name="my-prompt",
        optimizer_config={"source_type": "prompt_target", "model": None, "bound_to": None},
    )

    response = client.get(LIST_PATH)
    assert response.status_code == 200
    agent_ids = {item["agent_id"] for item in response.json()["data"]}
    assert agent_ids == {"real-agent"}
    assert "my-prompt" not in agent_ids


def test_list_agents_excludes_hidden_skill_targets(client: TestClient, db_session: Session) -> None:
    """Auto-provisioned hidden skill targets are a runtime detail — never agents."""
    _insert_agent(db_session, agent_id="real-agent", experiment_id="exp-real")
    # A hidden skill-target row (source_type marker + skill:: prefix).
    _insert_agent(
        db_session,
        agent_id="skill::my-skill",
        experiment_id="skill-target-abc123",
        name="my-skill",
        optimizer_config={
            "source_type": "skill_target",
            "skill_name": "my-skill",
            "bound_to": None,
        },
    )

    response = client.get(LIST_PATH)
    assert response.status_code == 200
    agent_ids = {item["agent_id"] for item in response.json()["data"]}
    assert agent_ids == {"real-agent"}
    assert "skill::my-skill" not in agent_ids


def test_get_agent_returns_full_record(client: TestClient, db_session: Session) -> None:
    _insert_agent(
        db_session,
        agent_id="support-agent",
        experiment_id="exp-support-prod",
        artifact_types=["prompt", "guardrail"],
        eval_thresholds={"min_aggregate_score": 0.85},
        optimizer_config={"type": "MetaPrompt", "params": {"temperature": 0.7}},
    )

    response = client.get(DETAIL_PATH.replace("{agent_id}", "support-agent"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["agent_id"] == "support-agent"
    assert data["experiment_id"] == "exp-support-prod"
    assert data["artifact_types"] == ["prompt", "guardrail"]
    assert data["eval_thresholds"] == {"min_aggregate_score": 0.85}
    assert data["optimizer_config"] == {"type": "MetaPrompt", "params": {"temperature": 0.7}}
    assert data["optimize_for"] == "quality"


def test_get_agent_404_when_missing(client: TestClient) -> None:
    response = client.get(DETAIL_PATH.replace("{agent_id}", "does-not-exist"))
    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /agents — register
# ---------------------------------------------------------------------------


def test_register_agent_creates_record(client: TestClient, db_session: Session) -> None:
    response = client.post(
        LIST_PATH,
        json={
            "agent_id": "new-agent",
            "experiment_id": "exp-new",
            "name": "New Agent",
            "owner": "@alex",
            "artifact_types": ["prompt"],
            "required_approvals": 2,
        },
    )
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["agent_id"] == "new-agent"
    assert body["required_approvals"] == 2
    assert body["enabled"] is True

    db_session.expire_all()
    row = db_session.get(CaliberAgentConfig, "new-agent")
    assert row is not None
    assert row.experiment_id == "exp-new"


def test_register_agent_rejects_duplicate_agent_id(client: TestClient, db_session: Session) -> None:
    _insert_agent(db_session, agent_id="dup", experiment_id="exp-dup")
    response = client.post(
        LIST_PATH,
        json={
            "agent_id": "dup",
            "experiment_id": "exp-different",
            "name": "Dup",
            "owner": "@x",
        },
    )
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


def test_register_agent_rejects_duplicate_experiment_id(
    client: TestClient, db_session: Session
) -> None:
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-shared")
    response = client.post(
        LIST_PATH,
        json={
            "agent_id": "a-2",
            "experiment_id": "exp-shared",
            "name": "Two",
            "owner": "@x",
        },
    )
    assert response.status_code == 409
    assert "experiment_id" in response.json()["detail"]


def test_register_agent_rejects_invalid_required_approvals(client: TestClient) -> None:
    response = client.post(
        LIST_PATH,
        json={
            "agent_id": "a-1",
            "experiment_id": "exp-1",
            "name": "One",
            "owner": "@x",
            "required_approvals": 0,
        },
    )
    assert response.status_code == 400
    assert "required_approvals" in response.text


# ---------------------------------------------------------------------------
# PATCH /agents/{id} — update / pause / resume
# ---------------------------------------------------------------------------


def test_patch_agent_pauses_and_resumes(client: TestClient, db_session: Session) -> None:
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1")

    response = client.patch(DETAIL_PATH.replace("{agent_id}", "a-1"), json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["data"]["enabled"] is False

    response = client.patch(DETAIL_PATH.replace("{agent_id}", "a-1"), json={"enabled": True})
    assert response.status_code == 200
    assert response.json()["data"]["enabled"] is True


def test_patch_agent_records_diff_in_audit_log(client: TestClient, db_session: Session) -> None:
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1", name="Old")

    response = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "New Name", "required_approvals": 3},
    )
    assert response.status_code == 200

    db_session.expire_all()
    audit_rows = (
        db_session.query(CaliberAuditLog).filter_by(action="update_agent", entity_id="a-1").all()
    )
    assert len(audit_rows) == 1
    changes = audit_rows[0].details["changes"]  # type: ignore[index]
    assert changes["name"] == {"from": "Old", "to": "New Name"}
    assert changes["required_approvals"] == {"from": 1, "to": 3}


def test_patch_agent_404_when_missing(client: TestClient) -> None:
    response = client.patch(DETAIL_PATH.replace("{agent_id}", "ghost"), json={"enabled": False})
    assert response.status_code == 404


def test_patch_agent_400_when_body_empty(client: TestClient, db_session: Session) -> None:
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1")
    response = client.patch(DETAIL_PATH.replace("{agent_id}", "a-1"), json={})
    assert response.status_code == 400


def test_patch_agent_no_op_when_values_unchanged(client: TestClient, db_session: Session) -> None:
    """Sending the same values currently in the DB should not write an audit row."""
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1", name="Same")
    response = client.patch(DETAIL_PATH.replace("{agent_id}", "a-1"), json={"name": "Same"})
    assert response.status_code == 200
    assert (
        db_session.query(CaliberAuditLog).filter_by(action="update_agent", entity_id="a-1").count()
        == 0
    )


# ---------------------------------------------------------------------------
# DELETE /agents/{agent_id}
# ---------------------------------------------------------------------------


def test_delete_agent_removes_record_and_audits(client: TestClient, db_session: Session) -> None:
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1", name="Throwaway")

    response = client.delete(DETAIL_PATH.replace("{agent_id}", "a-1"))
    assert response.status_code == 200
    assert response.json()["data"] == {"agent_id": "a-1", "deleted": True}

    # Gone from the registry and a 404 on a follow-up read.
    assert client.get(DETAIL_PATH.replace("{agent_id}", "a-1")).status_code == 404
    assert db_session.get(CaliberAgentConfig, "a-1") is None
    assert (
        db_session.query(CaliberAuditLog).filter_by(action="delete_agent", entity_id="a-1").count()
        == 1
    )


def test_delete_agent_404_when_missing(client: TestClient) -> None:
    response = client.delete(DETAIL_PATH.replace("{agent_id}", "ghost"))
    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]


def test_delete_agent_cascades_dependent_rows(client: TestClient, db_session: Session) -> None:
    """Deleting an agent clears the verification/refinement rows that FK to it."""
    _insert_agent(db_session, agent_id="a-1", experiment_id="exp-1")
    db_session.add(
        CaliberVerificationItem(
            item_id="FB-1",
            agent_id="a-1",
            category="bug",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    db_session.flush()
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-1",
            agent_id="a-1",
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="queued",
            current_stage="triage",
            bundle_targets=[],
        )
    )
    db_session.commit()

    response = client.delete(DETAIL_PATH.replace("{agent_id}", "a-1"))
    assert response.status_code == 200

    assert db_session.get(CaliberAgentConfig, "a-1") is None
    assert db_session.query(CaliberRefinementJob).filter_by(agent_id="a-1").count() == 0
    assert db_session.query(CaliberVerificationItem).filter_by(agent_id="a-1").count() == 0
