"""Tests for the cross-artifact Releases surface (/caliber/releases/*)."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberKnowledgeBase,
    CaliberReleaseOperation,
    CaliberWorkflowDeployment,
)
from caliber.release_operations import prepare_prompt_alias_release

TIMELINE = "/ajax-api/2.0/mlflow/caliber/releases/timeline"
LIVE = "/ajax-api/2.0/mlflow/caliber/releases/live"
OPERATIONS = "/ajax-api/2.0/mlflow/caliber/releases/operations"


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


def test_live_kb_since_and_by_come_from_activation_audit(
    client: TestClient, db_session: Session
) -> None:
    """A KB's live ``since``/``by`` reflect when/who activated the live version.

    ``updated_at`` is bumped on any edit (a rename, a re-index) and ``owner`` is
    the KB's owner — neither answers "when did this version go live, and who put
    it there". Those must come from the activation audit row for the live version.
    """
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-ACT",
            name="corpus",
            source_bucket="b",
            owner="@owner",
            active_version_id="KBV-2",
        )
    )
    # A KB whose active version was never audited (build-time activation of the
    # first version) falls back to owner.
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-FALLBACK",
            name="legacy",
            source_bucket="b",
            owner="@legacy-owner",
            active_version_id="KBV-legacy",
        )
    )
    # An older activation (of KBV-1) and the current one (of KBV-2). Only the
    # row naming the *live* version (KBV-2) should drive since/by.
    db_session.add(
        CaliberAuditLog(
            actor="@someone-else",
            action="activate_knowledge_base_version",
            entity_type="knowledge_base",
            entity_id="KB-ACT",
            details={"version_id": "KBV-1", "previous_active_version_id": None},
        )
    )
    db_session.add(
        CaliberAuditLog(
            actor="@activator",
            action="activate_knowledge_base_version",
            entity_type="knowledge_base",
            entity_id="KB-ACT",
            details={"version_id": "KBV-2", "previous_active_version_id": "KBV-1"},
        )
    )
    db_session.commit()

    rows = client.get(LIVE).json()["data"]
    by_id = {r["artifact_id"]: r for r in rows}

    # Audited activation drives by/since — the activator, not @owner, and not the
    # actor of the stale KBV-1 activation.
    assert by_id["KB-ACT"]["by"] == "@activator"
    assert by_id["KB-ACT"]["since"] is not None

    # No audited activation -> fall back to the KB owner.
    assert by_id["KB-FALLBACK"]["by"] == "@legacy-owner"


def test_operator_can_abandon_a_prepared_release(client: TestClient, db_session: Session) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-stale-prepared",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )

    response = client.post(
        f"{OPERATIONS}/{operation.operation_id}/resolve",
        json={"action": "abandon", "reason": "operator verified no provider call"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "failed"
    db_session.expire_all()
    row = db_session.get(CaliberReleaseOperation, operation.operation_id)
    assert row is not None and row.active_lock is None


def test_operator_can_retry_the_exact_prepared_release(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-retry-prepared",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )
    calls: list[dict[str, object]] = []

    def mutate_alias(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return dict(kwargs)

    monkeypatch.setattr("caliber.routes.prompts.set_prompt_alias_version", mutate_alias)
    response = client.post(
        f"{OPERATIONS}/{operation.operation_id}/resolve",
        json={"action": "retry"},
    )

    assert response.status_code == 200, response.text
    assert calls == [{"name": "p-retry-prepared", "alias": "prod", "version": 2}]
    assert response.json()["data"]["operation_id"] == operation.operation_id


def test_applying_release_refuses_blind_retry(client: TestClient, db_session: Session) -> None:
    operation = prepare_prompt_alias_release(
        db_session,
        name="p-ambiguous",
        alias="prod",
        version_before=1,
        version_after=2,
        actor="@operator",
    )
    operation.status = "applying"
    db_session.commit()

    response = client.post(
        f"{OPERATIONS}/{operation.operation_id}/resolve",
        json={"action": "retry"},
    )

    assert response.status_code == 409
    assert "reconciliation" not in response.text.lower() or "only prepared" in response.text.lower()
