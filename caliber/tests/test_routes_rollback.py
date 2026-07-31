"""Integration tests for ``/caliber/agents/{agent_id}/rollback*``."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberSkill,
    CaliberSkillVersion,
    CaliberVerificationItem,
)


def _seed_agent(session: Session, agent_id: str = "support-agent") -> None:
    session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"exp-{agent_id}",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.commit()


def _seed_checkpoint(
    session: Session,
    checkpoint_id: str = "CK-1",
    agent_id: str = "support-agent",
    version_after: int = 2,
    rolled_back_at: datetime | None = None,
) -> CaliberRollbackCheckpoint:
    # Approval and item rows aren't really needed for the rollback path, but
    # the FK constraints require them.
    session.add(
        CaliberVerificationItem(
            item_id=f"FB-{checkpoint_id}",
            agent_id=agent_id,
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=f"RFN-{checkpoint_id}",
            agent_id=agent_id,
            primary_item_id=f"FB-{checkpoint_id}",
            artifact_type="prompt",
            status="completed",
            current_stage="done",
            bundle_targets=[],
        )
    )
    session.flush()
    session.add(
        CaliberApprovalRequest(
            approval_id=f"AP-{checkpoint_id}",
            job_id=f"RFN-{checkpoint_id}",
            agent_id=agent_id,
            status="approved",
            eval_results={},
            candidate_snapshot={"content": "v"},
            diagnosis_snapshot=None,
        )
    )
    session.flush()
    checkpoint = CaliberRollbackCheckpoint(
        checkpoint_id=checkpoint_id,
        approval_id=f"AP-{checkpoint_id}",
        agent_id=agent_id,
        artifact_type="prompt",
        artifact_name=agent_id,
        artifact_ref_before=(
            f"prompts:/{agent_id}/{version_after - 1}" if version_after > 1 else None
        ),
        artifact_ref_after=f"prompts:/{agent_id}/{version_after}",
        version_before=version_after - 1 if version_after > 1 else None,
        version_after=version_after,
        rolled_back_at=rolled_back_at,
    )
    session.add(checkpoint)
    session.commit()
    return checkpoint


def test_list_checkpoints_empty_for_new_agent(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    response = client.get("/ajax-api/2.0/mlflow/caliber/agents/support-agent/checkpoints")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_list_checkpoints_404_when_agent_missing(client: TestClient) -> None:
    response = client.get("/ajax-api/2.0/mlflow/caliber/agents/ghost/checkpoints")
    assert response.status_code == 404


def test_list_checkpoints_returns_newest_first(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    _seed_checkpoint(db_session, checkpoint_id="CK-1", version_after=2)
    _seed_checkpoint(db_session, checkpoint_id="CK-2", version_after=3)
    response = client.get("/ajax-api/2.0/mlflow/caliber/agents/support-agent/checkpoints")
    assert response.status_code == 200
    ids = [row["checkpoint_id"] for row in response.json()["data"]]
    assert ids[0] == "CK-2"  # newer first
    assert "CK-1" in ids


def test_rollback_uses_most_recent_unused_checkpoint(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session)
    _seed_checkpoint(
        db_session,
        checkpoint_id="CK-OLD",
        version_after=2,
        rolled_back_at=datetime.now(timezone.utc),
    )
    _seed_checkpoint(db_session, checkpoint_id="CK-NEW", version_after=3)

    response = client.post("/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["checkpoint"]["checkpoint_id"] == "CK-NEW"
    assert data["checkpoint"]["rolled_back_at"] is not None
    # Rotated_to should point at the prior version (v2).
    assert "v2" in data["rotated_to"] or "/2" in data["rotated_to"]


def test_rollback_target_specific_checkpoint(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    _seed_checkpoint(db_session, checkpoint_id="CK-1", version_after=2)
    _seed_checkpoint(db_session, checkpoint_id="CK-2", version_after=3)

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": "CK-1"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["checkpoint"]["checkpoint_id"] == "CK-1"


def test_rollback_404_when_no_checkpoints(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    response = client.post("/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback")
    assert response.status_code == 404


def test_rollback_404_when_agent_missing(client: TestClient) -> None:
    response = client.post("/ajax-api/2.0/mlflow/caliber/agents/ghost/rollback")
    assert response.status_code == 404


def test_rollback_404_when_checkpoint_belongs_to_different_agent(
    client: TestClient, db_session: Session
) -> None:
    """Cross-agent guard (deep-review Finding 2).

    A reviewer hitting ``/agents/A/rollback`` with a ``checkpoint_id``
    that actually belongs to agent ``B`` previously rolled back
    ``B`` while the audit row claimed ``A`` — a cross-resource
    authorization defect. The endpoint must 404 instead so the
    caller knows the request can't be served.
    """
    _seed_agent(db_session, agent_id="agent-A")
    _seed_agent(db_session, agent_id="agent-B")
    _seed_checkpoint(
        db_session,
        checkpoint_id="CK-belongs-to-B",
        agent_id="agent-B",
        version_after=2,
    )

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/agent-A/rollback",
        json={"checkpoint_id": "CK-belongs-to-B"},
    )
    assert response.status_code == 404

    # The B-side checkpoint must remain untouched — no ``rolled_back_at``.
    db_session.expire_all()
    cp = db_session.get(CaliberRollbackCheckpoint, "CK-belongs-to-B")
    assert cp is not None
    assert cp.rolled_back_at is None

    # And no audit row should have been written for either agent.
    audit_rows = (
        db_session.query(CaliberAuditLog).filter(CaliberAuditLog.action == "rollback").all()
    )
    assert audit_rows == []


def test_rollback_rejects_non_string_checkpoint_id(client: TestClient, db_session: Session) -> None:
    """Backend V2 Finding 1: ``{"checkpoint_id": 123}`` previously
    silently fell through to the "latest unused checkpoint" path,
    rolling back a different target than the caller named. We now
    validate the type/shape and 400 the request — the original
    checkpoint stays unrotated so an audit trail isn't muddied."""
    _seed_agent(db_session)
    _seed_checkpoint(db_session, checkpoint_id="CK-LATEST", version_after=2)

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": 123},
    )
    assert response.status_code == 400
    assert "checkpoint_id" in response.json()["detail"]

    # The would-be-rolled-back row is still untouched.
    db_session.expire_all()
    cp = db_session.get(CaliberRollbackCheckpoint, "CK-LATEST")
    assert cp is not None
    assert cp.rolled_back_at is None


def test_rollback_rejects_empty_checkpoint_id(client: TestClient, db_session: Session) -> None:
    """Empty string is also explicit-but-invalid — same 400 path."""
    _seed_agent(db_session)
    _seed_checkpoint(db_session, checkpoint_id="CK-LATEST", version_after=2)

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": ""},
    )
    assert response.status_code == 400


def test_rollback_409_when_already_rolled_back(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    _seed_checkpoint(
        db_session,
        checkpoint_id="CK-DONE",
        version_after=2,
        rolled_back_at=datetime.now(timezone.utc),
    )
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": "CK-DONE"},
    )
    assert response.status_code == 409


def test_rollback_writes_audit_row(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    _seed_checkpoint(db_session, checkpoint_id="CK-1", version_after=2)
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        headers={"X-CALIBER-User": "@admin"},
    )
    assert response.status_code == 200
    db_session.expire_all()
    rows = (
        db_session.query(CaliberAuditLog)
        .filter_by(action="rollback", entity_id="support-agent")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor == "@admin"
    assert rows[0].details["checkpoint_id"] == "CK-1"  # type: ignore[index]


def test_skill_rollback_restores_snapshot_as_new_version_atomically(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session, agent_id="support-agent")
    db_session.add(
        CaliberSkill(
            skill_id="SK-one",
            name="tool-use",
            description="",
            summary="Improved summary",
            content="IMPROVED",
            owner="@owner",
            tags=[],
            version=2,
        )
    )
    db_session.add_all(
        [
            CaliberSkillVersion(
                skill_version_id="SKV-one-v1",
                skill_id="SK-one",
                version_number=1,
                content="ORIGINAL",
                summary="Original summary",
                created_by="@owner",
            ),
            CaliberSkillVersion(
                skill_version_id="SKV-one-v2",
                skill_id="SK-one",
                version_number=2,
                content="IMPROVED",
                summary="Improved summary",
                created_by="@operator",
            ),
        ]
    )
    db_session.commit()
    checkpoint = _seed_checkpoint(
        db_session,
        checkpoint_id="CK-skill",
        agent_id="support-agent",
        version_after=2,
    )
    checkpoint.artifact_type = "skill"
    checkpoint.artifact_name = "tool-use"
    checkpoint.artifact_ref_before = "skill://tool-use/v1"
    checkpoint.artifact_ref_after = "skill://tool-use/v2"
    checkpoint.snapshot_payload = {
        "content_before": "ORIGINAL",
        "summary_before": "Original summary",
        "version_before": 1,
    }
    db_session.commit()

    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": "CK-skill"},
        headers={"X-CALIBER-User": "@admin"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["rotated_to"] == "skill://tool-use/v3"

    db_session.expire_all()
    skill = db_session.get(CaliberSkill, "SK-one")
    assert skill is not None
    assert (skill.content, skill.summary, skill.version) == (
        "ORIGINAL",
        "Original summary",
        3,
    )
    versions = (
        db_session.query(CaliberSkillVersion)
        .filter(CaliberSkillVersion.skill_id == "SK-one")
        .order_by(CaliberSkillVersion.version_number)
        .all()
    )
    assert [row.version_number for row in versions] == [1, 2, 3]
    assert versions[-1].created_by == "@admin"
    refreshed = db_session.get(CaliberRollbackCheckpoint, "CK-skill")
    assert refreshed is not None and refreshed.rolled_back_at is not None
    rollback_audit = (
        db_session.query(CaliberAuditLog)
        .filter_by(action="rollback", entity_id="support-agent")
        .one()
    )
    assert rollback_audit.details["restored_from_version"] == 1  # type: ignore[index]
    assert rollback_audit.details["new_live_version"] == 3  # type: ignore[index]

    repeat = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": "CK-skill"},
    )
    assert repeat.status_code == 409


def test_rollback_cold_start_returns_502(client: TestClient, db_session: Session) -> None:
    """Cold-start checkpoint (no prior version) cannot be rolled back —
    the promoter raises PromoterError, the endpoint returns 502."""
    _seed_agent(db_session)
    _seed_checkpoint(db_session, checkpoint_id="CK-COLD", version_after=1)
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/support-agent/rollback",
        json={"checkpoint_id": "CK-COLD"},
    )
    assert response.status_code == 502
    assert "no prior version" in response.json()["detail"]
