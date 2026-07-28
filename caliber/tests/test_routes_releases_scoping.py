"""Releases aggregation must not show more than the workspaces it summarizes.

``product-complete-report.md`` §5 gap 3 recorded that the Releases API "returns
global release audit/live workflow/KB rows without the visibility/project
predicates used by the underlying resource workspaces". Both endpoints called
``require_user`` and then selected every matching row, so any signed-in user saw
other projects' live deployments and promotion history.

These tests run as a non-admin (``@viewer`` is deliberately outside
``conftest._PERMISSIVE_TEST_USERS``) so the admin bypass cannot mask a regression.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberKnowledgeBase,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
)
from caliber.routes.releases import LIVE_PATH, TIMELINE_PATH

NON_ADMIN = "@viewer"
OTHER = "@someone-else"


def _workflow(session: Session, wid: str, owner: str, visibility: str) -> None:
    session.add(
        CaliberWorkflow(
            workflow_id=wid,
            name=wid,
            owner=owner,
            visibility=visibility,
            status="active",
        )
    )


def _deployment(session: Session, wid: str, dep_id: str) -> None:
    session.add(
        CaliberWorkflowDeployment(
            deployment_id=dep_id,
            workflow_id=wid,
            version_id=f"{wid}-v1",
            alias="prod",
            status="active",
        )
    )


def _kb(session: Session, kb_id: str, owner: str, visibility: str) -> None:
    session.add(
        CaliberKnowledgeBase(
            knowledge_base_id=kb_id,
            name=kb_id,
            owner=owner,
            status="active",
            visibility=visibility,
            source_bucket="kb-bucket",
            active_version_id=f"{kb_id}-v1",
        )
    )


def _audit(session: Session, action: str, entity_type: str, entity_id: str) -> None:
    session.add(
        CaliberAuditLog(
            timestamp=datetime.now(timezone.utc),
            actor=OTHER,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details={},
        )
    )


def test_live_hides_another_owners_deployment(client: TestClient, db_session: Session) -> None:
    """Deployments carry no visibility columns, so they scope via their workflow."""
    _workflow(db_session, "WF-mine", NON_ADMIN, "user")
    _workflow(db_session, "WF-theirs", OTHER, "user")
    _deployment(db_session, "WF-mine", "DEP-mine")
    _deployment(db_session, "WF-theirs", "DEP-theirs")
    db_session.commit()

    live = client.get(LIVE_PATH, headers={"X-CALIBER-User": NON_ADMIN}).json()["data"]
    wids = {e["artifact_id"] for e in live if e["artifact_type"] == "workflow"}

    assert "WF-mine" in wids
    assert "WF-theirs" not in wids


def test_live_hides_another_owners_knowledge_base(client: TestClient, db_session: Session) -> None:
    _kb(db_session, "KB-mine", NON_ADMIN, "user")
    _kb(db_session, "KB-theirs", OTHER, "user")
    db_session.commit()

    live = client.get(LIVE_PATH, headers={"X-CALIBER-User": NON_ADMIN}).json()["data"]
    ids = {e["artifact_id"] for e in live if e["artifact_type"] == "knowledge_base"}

    assert "KB-mine" in ids
    assert "KB-theirs" not in ids


def test_live_still_shows_public_rows(client: TestClient, db_session: Session) -> None:
    """Scoping must not hide genuinely shared state."""
    _kb(db_session, "KB-public", OTHER, "public")
    db_session.commit()

    live = client.get(LIVE_PATH, headers={"X-CALIBER-User": NON_ADMIN}).json()["data"]

    assert "KB-public" in {e["artifact_id"] for e in live}


def test_timeline_hides_events_for_invisible_entities(
    client: TestClient, db_session: Session
) -> None:
    _workflow(db_session, "WF-mine", NON_ADMIN, "user")
    _workflow(db_session, "WF-theirs", OTHER, "user")
    _audit(db_session, "promote_workflow", "workflow", "WF-mine")
    _audit(db_session, "promote_workflow", "workflow", "WF-theirs")
    db_session.commit()

    rows = client.get(TIMELINE_PATH, headers={"X-CALIBER-User": NON_ADMIN}).json()["data"]
    ids = {r["entity_id"] for r in rows}

    assert "WF-mine" in ids
    assert "WF-theirs" not in ids


def test_timeline_retains_prompt_events(client: TestClient, db_session: Session) -> None:
    """Documented residual: prompt liveness lives in MLflow, so prompt release
    rows have no local table to scope against and are deliberately retained."""
    _audit(db_session, "promote_prompt", "prompt", "some-prompt")
    db_session.commit()

    rows = client.get(TIMELINE_PATH, headers={"X-CALIBER-User": NON_ADMIN}).json()["data"]

    assert "some-prompt" in {r["entity_id"] for r in rows}


def test_admin_keeps_the_unfiltered_aggregate(client: TestClient, db_session: Session) -> None:
    """The default admin fixture must still see everything."""
    _workflow(db_session, "WF-theirs", OTHER, "user")
    _deployment(db_session, "WF-theirs", "DEP-theirs")
    _audit(db_session, "promote_workflow", "workflow", "WF-theirs")
    db_session.commit()

    live = client.get(LIVE_PATH).json()["data"]
    rows = client.get(TIMELINE_PATH).json()["data"]

    assert "WF-theirs" in {e["artifact_id"] for e in live}
    assert "WF-theirs" in {r["entity_id"] for r in rows}
