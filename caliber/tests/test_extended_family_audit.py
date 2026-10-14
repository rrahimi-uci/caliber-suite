"""Every content-changing action of the extended families leaves an audit row (alpha gap P1-24).

``docs/caliber-alpha-gaps.md`` asks that each extended family's create, publish or promote,
and rollback actions write an audit row. Prompts already do (P1-02). This file drives the
real routes for skills, tools, workflows and knowledge bases as the project's owner and
reads ``caliber_audit_log`` back: the row must exist, name the actor and carry the entity
the action changed.

An audit trail you cannot test is one nobody notices losing. Each assertion below fails if
the route stops writing its row.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog
from tests.workflow_helpers import (
    make_support_manifest,
    make_tool_payload,
    relax_release_graded_executor,
    relax_release_quality_gate,
    relax_tool_isolation_gate,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
ACTOR = "@test"  # the suite's default admin user


def _audit(db_session: Session, action: str, entity_id: str | None = None) -> list[CaliberAuditLog]:
    db_session.expire_all()
    stmt = select(CaliberAuditLog).where(CaliberAuditLog.action == action)
    if entity_id is not None:
        stmt = stmt.where(CaliberAuditLog.entity_id == entity_id)
    return list(db_session.execute(stmt).scalars().all())


def _assert_audited(
    db_session: Session,
    action: str,
    entity_id: str | None = None,
    *,
    entity_type: str | None = None,
) -> CaliberAuditLog:
    rows = _audit(db_session, action, entity_id)
    assert rows, f"no audit row for {action!r}" + (f" on {entity_id!r}" if entity_id else "")
    row = rows[-1]
    assert row.actor == ACTOR, f"{action}: audited actor was {row.actor!r}"
    if entity_type is not None:
        assert row.entity_type == entity_type, f"{action}: entity_type {row.entity_type!r}"
    return row


def _ok(response: Any, *statuses: int) -> dict[str, Any]:
    assert response.status_code in (statuses or (200, 201)), response.text
    return response.json()["data"]  # type: ignore[no-any-return]


def test_a_skill_create_update_and_rollback_are_audited(
    client: TestClient, db_session: Session
) -> None:
    skill = _ok(
        client.post(
            f"{PREFIX}/skills",
            json={
                "name": "audit-skill",
                "description": "d",
                "summary": "s",
                "content": "v1 {{x}}",
                "owner": ACTOR,
            },
        )
    )
    skill_id = skill["skill_id"]
    _assert_audited(db_session, "create_skill", skill_id, entity_type="skill")

    _ok(client.patch(f"{PREFIX}/skills/{skill_id}", json={"content": "v2 {{x}}"}))
    _assert_audited(db_session, "update_skill", skill_id, entity_type="skill")

    _ok(client.post(f"{PREFIX}/skills/{skill_id}/rollback", json={"to_version": 1}))
    row = _assert_audited(db_session, "rollback_skill", skill_id, entity_type="skill")
    assert row.details, "the rollback row carries no details"


def test_a_tool_register_update_and_archive_are_audited(
    client: TestClient, db_session: Session
) -> None:
    tool = _ok(client.post(f"{PREFIX}/tools", json=make_tool_payload("audit_tool")))
    tool_id = tool["tool_id"]
    _assert_audited(db_session, "register_tool", tool_id, entity_type="tool")

    _ok(client.patch(f"{PREFIX}/tools/{tool_id}", json={"description": "changed"}))
    _assert_audited(db_session, "update_tool", tool_id, entity_type="tool")

    _ok(client.post(f"{PREFIX}/tools/{tool_id}/archive", json={}))
    _assert_audited(db_session, "archive_tool", tool_id, entity_type="tool")


def test_a_workflow_is_audited_from_creation_to_rollback(
    client: TestClient, db_session: Session
) -> None:
    """Create, version, publish, promote, roll back and expose as a service."""
    for name in ("lookup_policy", "get_order", "escalate"):
        side_effect = "external_action" if name == "escalate" else "read"
        _ok(
            client.post(
                f"{PREFIX}/tools",
                json=make_tool_payload(name, allow_in_preview=True, side_effect_level=side_effect),
            )
        )
    workflow_id = _ok(
        client.post(f"{PREFIX}/workflows", json={"name": "audit-workflow", "owner": ACTOR})
    )["workflow_id"]
    _assert_audited(db_session, "create_workflow", workflow_id)

    manifest = make_support_manifest(workflow_id)
    first = _ok(
        client.post(f"{PREFIX}/workflows/{workflow_id}/versions", json={"manifest": manifest})
    )["version_id"]
    _assert_audited(db_session, "create_workflow_version", first)

    _ok(client.post(f"{PREFIX}/workflow-versions/{first}/publish"))
    _assert_audited(db_session, "publish_workflow_version", first)

    relax_release_quality_gate(client)
    relax_release_graded_executor(client)
    relax_tool_isolation_gate(client)
    _ok(
        client.post(
            f"{PREFIX}/workflows/{workflow_id}/deployments/prod/promote",
            json={"version_id": first},
        )
    )
    promoted = _assert_audited(db_session, "promote_workflow", workflow_id)
    assert promoted.details.get("alias") == "prod"

    second = _ok(
        client.post(f"{PREFIX}/workflows/{workflow_id}/versions", json={"manifest": manifest})
    )["version_id"]
    _ok(client.post(f"{PREFIX}/workflow-versions/{second}/publish"))
    _ok(
        client.post(
            f"{PREFIX}/workflows/{workflow_id}/deployments/prod/promote",
            json={"version_id": second},
        )
    )
    _ok(client.post(f"{PREFIX}/workflows/{workflow_id}/deployments/prod/rollback", json={}))
    rolled_back = _assert_audited(db_session, "rollback_workflow", workflow_id)
    assert rolled_back.details.get("version_id") == first

    service = _ok(client.post(f"{PREFIX}/workflows/{workflow_id}/service", json={}), 200)
    assert _audit(db_session, "publish_workflow_service") or _audit(
        db_session, "update_workflow_service"
    ), f"publishing {service['service_id']!r} was not audited"
    token = _ok(client.post(f"{PREFIX}/workflows/{workflow_id}/service/tokens", json={"name": "t"}))
    _assert_audited(db_session, "create_service_token", token["token_id"])
    client.delete(f"{PREFIX}/workflows/{workflow_id}/service/tokens/{token['token_id']}")
    _assert_audited(db_session, "revoke_service_token", token["token_id"])
    client.delete(f"{PREFIX}/workflows/{workflow_id}/service")
    _assert_audited(db_session, "unpublish_workflow_service")


@pytest.mark.usefixtures("gated_prod")
def test_a_gated_promotion_is_audited_when_requested_and_when_decided(
    client: TestClient, db_session: Session
) -> None:
    """A promotion that waits for approval leaves a row for the request and for the decision."""
    for name in ("lookup_policy", "get_order", "escalate"):
        side_effect = "external_action" if name == "escalate" else "read"
        _ok(
            client.post(
                f"{PREFIX}/tools",
                json=make_tool_payload(name, allow_in_preview=True, side_effect_level=side_effect),
            )
        )
    workflow_id = _ok(
        client.post(f"{PREFIX}/workflows", json={"name": "audit-gated", "owner": ACTOR})
    )["workflow_id"]
    version = _ok(
        client.post(
            f"{PREFIX}/workflows/{workflow_id}/versions",
            json={"manifest": make_support_manifest(workflow_id)},
        )
    )["version_id"]
    _ok(client.post(f"{PREFIX}/workflow-versions/{version}/publish"))
    relax_release_quality_gate(client)
    relax_release_graded_executor(client)
    relax_tool_isolation_gate(client)

    response = client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/prod/promote",
        json={"version_id": version},
    )
    assert response.status_code == 202, response.text
    promotion_id = response.json()["data"]["promotion"]["promotion_id"]
    _assert_audited(db_session, "request_workflow_promotion", workflow_id)

    _ok(client.post(f"{PREFIX}/workflow-promotions/{promotion_id}/approve", json={}))
    _assert_audited(db_session, "approve_workflow_promotion")


def test_a_knowledge_base_create_is_audited(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activate and rollback are audited by tests/test_cov90_knowledge_service.py and
    tests/test_routes_releases.py; this adds the create, the third action the issue names."""
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from tests.test_routes_knowledge_bases import (
        _kb_create_payload,
        _wire_kb_create,
    )

    del boto3
    with moto.mock_aws():
        _wire_kb_create(client, monkeypatch, "audit-kb-bucket")
        created = _ok(
            client.post(
                f"{PREFIX}/knowledge-bases", json=_kb_create_payload("Audit KB", "audit-kb-bucket")
            )
        )
    kb_id = created["knowledge_base"]["knowledge_base_id"]
    _assert_audited(db_session, "create_knowledge_base", kb_id)
