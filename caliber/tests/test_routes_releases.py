"""Tests for the cross-artifact Releases surface (/caliber/releases/*)."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberKnowledgeBase,
    CaliberWorkflowDeployment,
)

TIMELINE = "/ajax-api/2.0/mlflow/caliber/releases/timeline"
LIVE = "/ajax-api/2.0/mlflow/caliber/releases/live"


def _audit(session: Session, action: str, entity_type: str, entity_id: str) -> None:
    session.add(
        CaliberAuditLog(
            actor="@op",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details={"alias": "prod"},
        )
    )


def test_timeline_returns_release_events_newest_first(
    client: TestClient, db_session: Session
) -> None:
    _audit(db_session, "promote_prompt", "prompt", "support-agent")
    _audit(db_session, "update_skill", "skill", "SK-1")  # not a release event
    _audit(db_session, "promote_workflow", "workflow", "WF-1")
    db_session.commit()

    resp = client.get(TIMELINE)
    assert resp.status_code == 200
    rows = resp.json()["data"]
    actions = [r["action"] for r in rows]
    # Only release events, newest (promote_workflow) first; update_skill excluded.
    assert actions == ["promote_workflow", "promote_prompt"]


def test_timeline_filters_by_entity_type_and_limit(client: TestClient, db_session: Session) -> None:
    _audit(db_session, "promote_prompt", "prompt", "p1")
    _audit(db_session, "promote_workflow", "workflow", "w1")
    db_session.commit()

    only_prompt = client.get(TIMELINE, params={"entity_type": "prompt"}).json()["data"]
    assert [r["entity_id"] for r in only_prompt] == ["p1"]

    limited = client.get(TIMELINE, params={"limit": "1"}).json()["data"]
    assert len(limited) == 1

    assert client.get(TIMELINE, params={"limit": "0"}).status_code == 400


def test_live_lists_active_workflow_deployments_and_kb_active_versions(
    client: TestClient, db_session: Session
) -> None:
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="DEP-L",
            workflow_id="WF-L",
            alias="prod",
            version_id="WFV-L",
            status="active",
        )
    )
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-L",
            name="corpus",
            source_bucket="b",
            active_version_id="KBV-L",
        )
    )
    db_session.commit()

    rows = client.get(LIVE).json()["data"]
    by_type = {r["artifact_type"]: r for r in rows}
    assert by_type["workflow"]["version_id"] == "WFV-L"
    assert by_type["knowledge_base"]["version_id"] == "KBV-L"
