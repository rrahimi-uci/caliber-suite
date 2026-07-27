"""Coverage-focused tests for ``caliber.routes.services`` (deploy-as-a-service).

Targets specific branches left uncovered by the main integration suite in
``tests/test_routes_services.py``: not-found/conflict guard clauses on the
internal (operator/admin) routes, the disabled-event-bus best-effort fanout,
the dormant Bearer-token auth path's expired/missing-scope branches, the
run-status projection helpers (``_run_output``/``_summary_str``), and the
``_input_to_text`` branches that are unreachable through the HTTP surface
because ``ServiceInvokeRequest.input`` is always parsed into a ``dict``.

Most tests seed exactly the DB rows a given branch needs directly via
``db_session`` rather than driving the full workflow-authoring flow, which
keeps each test focused on the one guard clause it exercises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberServiceToken,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowService,
)
from caliber.ids import (
    new_service_id,
    new_service_token_id,
    new_workflow_deployment_id,
    new_workflow_run_id,
)
from caliber.routes.services import _hash_token, _input_to_text
from tests.workflow_helpers import PREFIX, create_and_publish, deploy_prod


def _publish(client: TestClient, workflow_id: str, **body: object) -> dict[str, object]:
    r = client.post(f"{PREFIX}/workflows/{workflow_id}/service", json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _seed_service(
    db_session: Session, workflow_id: str, **overrides: object
) -> CaliberWorkflowService:
    kwargs: dict[str, object] = {
        "service_id": new_service_id(),
        "workflow_id": workflow_id,
        "alias": "prod",
        "input_schema": {},
        "output_schema": {},
        "enabled": True,
        "auth_required": False,
        "created_by": "@test",
    }
    kwargs.update(overrides)
    service = CaliberWorkflowService(**kwargs)
    db_session.add(service)
    return service


def _seed_run(db_session: Session, workflow_id: str, **overrides: object) -> CaliberWorkflowRun:
    kwargs: dict[str, object] = {
        "workflow_run_id": new_workflow_run_id(),
        "workflow_id": workflow_id,
        "status": "completed",
    }
    kwargs.update(overrides)
    run = CaliberWorkflowRun(**kwargs)
    db_session.add(run)
    return run


def _seed_token(
    db_session: Session, workflow_id: str, plaintext: str, **overrides: object
) -> CaliberServiceToken:
    kwargs: dict[str, object] = {
        "token_id": new_service_token_id(),
        "workflow_id": workflow_id,
        "name": "ci",
        "token_hash": _hash_token(plaintext),
        "prefix": plaintext[:16],
        "scopes": ["invoke"],
        "created_by": "@test",
    }
    kwargs.update(overrides)
    token = CaliberServiceToken(**kwargs)
    db_session.add(token)
    return token


# ---------------------------------------------------------------------------
# publish_service — 404 unknown workflow (152-153), 409 missing version (165),
# and the update-existing-service branch (201-208).
# ---------------------------------------------------------------------------


def test_publish_service_returns_404_for_unknown_workflow(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflows/does-not-exist/service", json={})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_publish_service_returns_409_when_deployment_points_to_missing_version(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-ghost-version"
    db_session.add(
        CaliberWorkflow(workflow_id=workflow_id, name="Ghost", owner="@test", status="active")
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id=new_workflow_deployment_id(),
            workflow_id=workflow_id,
            alias="prod",
            version_id="does-not-exist",
            status="active",
        )
    )
    db_session.commit()
    r = client.post(f"{PREFIX}/workflows/{workflow_id}/service", json={})
    assert r.status_code == 409
    assert "missing version" in r.json()["detail"].lower()


def test_publish_service_updates_existing_service_and_audits(
    client: TestClient, db_session: Session
) -> None:
    wid, vid = create_and_publish(client)
    deploy_prod(client, wid, vid)

    first = _publish(client, wid)
    assert first["enabled"] is True
    assert first["auth_required"] is True

    second = _publish(client, wid, enabled=False, auth_required=True)
    assert second["service_id"] == first["service_id"]
    assert second["enabled"] is False
    assert second["auth_required"] is True

    rows = (
        db_session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.entity_id == first["service_id"])
            .where(CaliberAuditLog.action == "update_workflow_service")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# _emit_queue_event — best-effort fanout swallows a failing event bus (90-91).
# ---------------------------------------------------------------------------


def test_invoke_swallows_event_bus_publish_failure(client: TestClient, caplog: object) -> None:
    wid, vid = create_and_publish(client)
    deploy_prod(client, wid, vid)
    _publish(client, wid, auth_required=False)

    class _FailingBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingBus()
    caplog.set_level(logging.WARNING, logger="caliber.routes.services")  # type: ignore[attr-defined]

    r = client.post(f"{PREFIX}/services/{wid}/invoke", json={"input": {"user_message": "hi"}})
    assert r.status_code == 202, r.text
    assert r.json()["data"]["run_id"]
    assert "failed to publish service queue event" in caplog.text  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# delete_service / create_service_token / revoke_service_token guard clauses.
# ---------------------------------------------------------------------------


def test_delete_service_returns_404_when_not_published(client: TestClient) -> None:
    workflow_id, _ = create_and_publish(client)
    r = client.delete(f"{PREFIX}/workflows/{workflow_id}/service")
    assert r.status_code == 404
    assert "no service published" in r.json()["detail"].lower()


def test_create_service_token_returns_409_when_not_published(client: TestClient) -> None:
    workflow_id, _ = create_and_publish(client)
    r = client.post(f"{PREFIX}/workflows/{workflow_id}/service/tokens", json={"name": "ci"})
    assert r.status_code == 409
    assert "publish it first" in r.json()["detail"].lower()


def test_revoke_service_token_returns_404_when_token_missing(client: TestClient) -> None:
    r = client.delete(f"{PREFIX}/workflows/some-wf/service/tokens/does-not-exist")
    assert r.status_code == 404


def test_revoke_service_token_returns_404_when_workflow_mismatch(
    client: TestClient, db_session: Session
) -> None:
    wid, vid = create_and_publish(client)
    deploy_prod(client, wid, vid)
    _publish(client, wid)
    token = _seed_token(db_session, wid, "cal_svc_mismatch")
    db_session.commit()

    r = client.delete(f"{PREFIX}/workflows/some-other-workflow/service/tokens/{token.token_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# invoke_service — _load_enabled_service (404/403), Bearer-token auth
# (401 expired / missing scope), and the workflow/deployment/version guard
# clauses (404/409/409).
# ---------------------------------------------------------------------------


def test_invoke_returns_404_when_service_not_published(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/services/does-not-exist/invoke", json={"input": {}})
    assert r.status_code == 404
    assert "no service published" in r.json()["detail"].lower()


def test_invoke_returns_403_when_service_disabled(client: TestClient, db_session: Session) -> None:
    workflow_id = "wf-disabled-service"
    _seed_service(db_session, workflow_id, enabled=False)
    db_session.commit()

    r = client.post(f"{PREFIX}/services/{workflow_id}/invoke", json={"input": {}})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


def test_invoke_returns_401_when_token_expired(client: TestClient, db_session: Session) -> None:
    workflow_id = "wf-expired-token"
    _seed_service(db_session, workflow_id, auth_required=True)
    plaintext = "cal_svc_expired_token"
    _seed_token(
        db_session, workflow_id, plaintext, expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    db_session.commit()

    r = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {}},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_invoke_returns_401_when_token_missing_scope(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-missing-scope"
    _seed_service(db_session, workflow_id, auth_required=True)
    plaintext = "cal_svc_no_scope"
    _seed_token(db_session, workflow_id, plaintext, scopes=["some_other_scope"])
    db_session.commit()

    r = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {}},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 401
    assert "scope" in r.json()["detail"].lower()


def test_invoke_returns_404_when_workflow_missing(client: TestClient, db_session: Session) -> None:
    workflow_id = "wf-ghost-workflow"
    _seed_service(db_session, workflow_id)
    db_session.commit()

    r = client.post(f"{PREFIX}/services/{workflow_id}/invoke", json={"input": {}})
    assert r.status_code == 404
    assert "workflow" in r.json()["detail"].lower()


def test_invoke_returns_409_when_no_active_deployment(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-no-active-deployment"
    db_session.add(
        CaliberWorkflow(workflow_id=workflow_id, name="No Deploy", owner="@test", status="active")
    )
    _seed_service(db_session, workflow_id)
    db_session.commit()

    r = client.post(f"{PREFIX}/services/{workflow_id}/invoke", json={"input": {}})
    assert r.status_code == 409
    assert "no active deployment" in r.json()["detail"].lower()


def test_invoke_returns_409_when_version_missing(client: TestClient, db_session: Session) -> None:
    workflow_id = "wf-invoke-ghost-version"
    db_session.add(
        CaliberWorkflow(workflow_id=workflow_id, name="No Version", owner="@test", status="active")
    )
    _seed_service(db_session, workflow_id)
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id=new_workflow_deployment_id(),
            workflow_id=workflow_id,
            alias="prod",
            version_id="does-not-exist",
            status="active",
        )
    )
    db_session.commit()

    r = client.post(f"{PREFIX}/services/{workflow_id}/invoke", json={"input": {}})
    assert r.status_code == 409
    assert "missing version" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# _input_to_text — unreachable through the HTTP surface because
# ``ServiceInvokeRequest.input`` is typed ``dict[str, Any]`` (pydantic never
# hands the route a ``None``/``str``/non-JSON-serializable value), so these
# branches are exercised as direct unit calls on the private helper.
# ---------------------------------------------------------------------------


def test_input_to_text_none_returns_empty_string() -> None:
    assert _input_to_text(None) == ""


def test_input_to_text_str_returns_value_unchanged() -> None:
    assert _input_to_text("already a string") == "already a string"


def test_input_to_text_non_json_serializable_falls_back_to_str() -> None:
    value = {1, 2, 3}  # a set is not JSON serializable -> json.dumps raises TypeError
    assert _input_to_text(value) == str(value)


# ---------------------------------------------------------------------------
# get_service_run_status — 404 unknown run, and the completed/failed output
# projection helpers (_run_output / _summary_str).
# ---------------------------------------------------------------------------


def test_get_run_status_returns_404_when_run_missing(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-run-status-404"
    _seed_service(db_session, workflow_id)
    db_session.commit()

    r = client.get(f"{PREFIX}/services/{workflow_id}/runs/does-not-exist")
    assert r.status_code == 404


def test_get_run_status_completed_with_dict_output_passes_through(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-completed-dict-output"
    _seed_service(db_session, workflow_id)
    run = _seed_run(db_session, workflow_id, status="completed", summary={"output": {"answer": 42}})
    db_session.commit()

    r = client.get(f"{PREFIX}/services/{workflow_id}/runs/{run.workflow_run_id}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "completed"
    assert data["output"] == {"answer": 42}
    assert data["error"] is None


def test_get_run_status_completed_with_scalar_output_is_wrapped(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-completed-scalar-output"
    _seed_service(db_session, workflow_id)
    run = _seed_run(db_session, workflow_id, status="completed", summary={"output": "plain text"})
    db_session.commit()

    r = client.get(f"{PREFIX}/services/{workflow_id}/runs/{run.workflow_run_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["output"] == {"output": "plain text"}


def test_get_run_status_failed_prefers_top_level_error_summary(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-failed-error-summary"
    _seed_service(db_session, workflow_id)
    run = _seed_run(
        db_session,
        workflow_id,
        status="failed",
        error_summary="boom from the top-level column",
        summary={"error": "should not be used"},
    )
    db_session.commit()

    r = client.get(f"{PREFIX}/services/{workflow_id}/runs/{run.workflow_run_id}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert data["error"] == "boom from the top-level column"


def test_get_run_status_failed_falls_back_to_summary_error(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-failed-summary-fallback"
    _seed_service(db_session, workflow_id)
    run = _seed_run(
        db_session, workflow_id, status="failed", error_summary=None, summary={"error": "boom"}
    )
    db_session.commit()

    r = client.get(f"{PREFIX}/services/{workflow_id}/runs/{run.workflow_run_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["error"] == "boom"


def test_get_run_status_failed_with_no_error_info_returns_none(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-failed-no-error-info"
    _seed_service(db_session, workflow_id)
    run = _seed_run(db_session, workflow_id, status="failed", error_summary=None, summary=None)
    db_session.commit()

    r = client.get(f"{PREFIX}/services/{workflow_id}/runs/{run.workflow_run_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["error"] is None


# ---------------------------------------------------------------------------
# service_openapi — auth_required branch adds the securitySchemes component
# and requires a valid Bearer token to view the spec.
# ---------------------------------------------------------------------------


def test_service_openapi_with_auth_required_includes_security_scheme(
    client: TestClient, db_session: Session
) -> None:
    workflow_id = "wf-openapi-auth"
    _seed_service(db_session, workflow_id, auth_required=True)
    plaintext = "cal_svc_openapi_token"
    _seed_token(db_session, workflow_id, plaintext)
    db_session.commit()

    r = client.get(
        f"{PREFIX}/services/{workflow_id}/openapi.json",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 200, r.text
    spec = r.json()  # raw OpenAPI (not enveloped)
    assert spec["components"]["securitySchemes"]["serviceToken"] == {
        "type": "http",
        "scheme": "bearer",
    }
    invoke_path = f"{PREFIX}/services/{workflow_id}/invoke"
    assert spec["paths"][invoke_path]["post"]["security"] == [{"serviceToken": []}]
