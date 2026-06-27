"""Integration tests for the deploy-workflow-as-a-service routes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    create_draft,
    create_workflow,
    deploy_prod,
    make_support_manifest,
    register_demo_tools,
)


def _publish_service(
    client: TestClient, *, auth_required: bool = False
) -> tuple[str, str]:
    """Create+publish a workflow, deploy it to prod, then publish the service.

    Services are OPEN by default (v1); pass ``auth_required=True`` to exercise the
    dormant Bearer-token path. Returns ``(workflow_id, version_id)``.
    """
    wid, vid = create_and_publish(client)
    deploy_prod(client, wid, vid)
    body = {"auth_required": True} if auth_required else {}
    r = client.post(f"{PREFIX}/workflows/{wid}/service", json=body)
    assert r.status_code == 200, r.text
    return wid, vid


def _mint_token(client: TestClient, workflow_id: str, name: str = "ci") -> str:
    r = client.post(
        f"{PREFIX}/workflows/{workflow_id}/service/tokens",
        json={"name": name},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def test_publish_requires_live_deployment(client: TestClient) -> None:
    # No deployment yet -> 409.
    register_demo_tools(client)
    wid = create_workflow(client)
    vid, _ = create_draft(client, wid, make_support_manifest(wid))
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    assert r.status_code == 200, r.text
    r = client.post(f"{PREFIX}/workflows/{wid}/service", json={})
    assert r.status_code == 409
    assert "live deployment" in r.json()["detail"].lower()

    # Deploy, then publish succeeds and derives non-empty I/O schema.
    deploy_prod(client, wid, vid)
    r = client.post(f"{PREFIX}/workflows/{wid}/service", json={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["alias"] == "prod"
    assert data["enabled"] is True
    assert data["auth_required"] is False  # v1: services are open by default
    assert data["input_schema"]["type"] == "object"
    assert data["input_schema"]["properties"]  # non-empty (start outputs)
    assert data["output_schema"]["properties"]  # non-empty (output inputs)
    assert data["endpoint"] == f"{PREFIX}/services/{wid}/invoke"
    assert data["token_count"] == 0


def test_get_service_returns_config(client: TestClient) -> None:
    wid, _ = _publish_service(client)
    r = client.get(f"{PREFIX}/workflows/{wid}/service")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["workflow_id"] == wid
    assert data["endpoint"] == f"{PREFIX}/services/{wid}/invoke"


def test_get_service_404_when_unpublished(client: TestClient) -> None:
    wid, vid = create_and_publish(client)
    deploy_prod(client, wid, vid)
    r = client.get(f"{PREFIX}/workflows/{wid}/service")
    assert r.status_code == 404


def test_mint_token_returns_plaintext_once_and_list_is_masked(client: TestClient) -> None:
    wid, _ = _publish_service(client)
    r = client.post(f"{PREFIX}/workflows/{wid}/service/tokens", json={"name": "ci"})
    assert r.status_code == 201, r.text
    created = r.json()["data"]
    assert created["token"].startswith("cal_svc_")
    assert created["name"] == "ci"
    assert created["scopes"] == ["invoke"]
    plaintext = created["token"]

    # List never returns the plaintext or the hash.
    r = client.get(f"{PREFIX}/workflows/{wid}/service/tokens")
    assert r.status_code == 200, r.text
    items = r.json()["data"]
    assert len(items) == 1
    masked = items[0]
    assert "token" not in masked
    assert "token_hash" not in masked
    assert masked["prefix"]
    assert masked["prefix"] in plaintext
    assert masked["revoked_at"] is None


def test_invoke_open_without_token_returns_202(client: TestClient) -> None:
    # v1: services are open — no token needed to invoke.
    wid, _ = _publish_service(client)
    r = client.post(f"{PREFIX}/services/{wid}/invoke", json={"input": {"user_message": "hi"}})
    assert r.status_code == 202, r.text
    data = r.json()["data"]
    assert data["status"] == "queued"
    assert data["run_id"]


def test_invoke_writes_audit_row(client: TestClient, db_session: Session) -> None:
    wid, _ = _publish_service(client)
    invoke = client.post(f"{PREFIX}/services/{wid}/invoke", json={"input": {"user_message": "hi"}})
    assert invoke.status_code == 202, invoke.text
    run_id = invoke.json()["data"]["run_id"]
    rows = (
        db_session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.entity_id == wid)
            .where(CaliberAuditLog.action == "invoke_workflow_service")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].details["run_id"] == run_id


def test_invoke_with_invalid_input_is_400(client: TestClient) -> None:
    # Input is validated regardless of auth. user_message is a typed string in the
    # manifest, so a number fails validation.
    wid, _ = _publish_service(client)
    r = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": 123}},
    )
    assert r.status_code == 400, r.text


def test_get_run_status_open(client: TestClient) -> None:
    wid, _ = _publish_service(client)
    invoke = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
    )
    assert invoke.status_code == 202, invoke.text
    run_id = invoke.json()["data"]["run_id"]

    r = client.get(f"{PREFIX}/services/{wid}/runs/{run_id}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["run_id"] == run_id
    assert data["status"] == "queued"


def test_auth_required_enforces_bearer_token(client: TestClient) -> None:
    # Opting a service into auth re-activates the dormant Bearer-token path.
    wid, _ = _publish_service(client, auth_required=True)

    # No token -> 401.
    r = client.post(f"{PREFIX}/services/{wid}/invoke", json={"input": {"user_message": "hi"}})
    assert r.status_code == 401

    # Valid token -> 202.
    token = _mint_token(client, wid)
    r = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hello"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["data"]["run_id"]

    # Revoked token -> 401.
    create_resp = client.post(f"{PREFIX}/workflows/{wid}/service/tokens", json={"name": "tmp"})
    revoked = create_resp.json()["data"]["token"]
    token_id = create_resp.json()["data"]["token_id"]
    assert client.delete(f"{PREFIX}/workflows/{wid}/service/tokens/{token_id}").status_code == 200
    r = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers={"Authorization": f"Bearer {revoked}"},
    )
    assert r.status_code == 401

    # Status endpoint is likewise gated when auth_required.
    r = client.get(f"{PREFIX}/services/{wid}/runs/whatever")
    assert r.status_code == 401


def test_service_openapi_spec(client: TestClient) -> None:
    wid, _ = _publish_service(client)
    r = client.get(f"{PREFIX}/services/{wid}/openapi.json")
    assert r.status_code == 200, r.text
    spec = r.json()  # raw OpenAPI (not enveloped)
    assert str(spec["openapi"]).startswith("3.")
    invoke_path = f"{PREFIX}/services/{wid}/invoke"
    assert invoke_path in spec["paths"]
    post = spec["paths"][invoke_path]["post"]
    body_schema = post["requestBody"]["content"]["application/json"]["schema"]
    # The service's derived input schema is embedded under `input`.
    assert body_schema["properties"]["input"]["type"] == "object"
    # Open service -> no security requirement.
    assert post.get("security") in (None, [])


def test_delete_service_unpublishes(client: TestClient) -> None:
    wid, _ = _publish_service(client)
    # Mint a token so we also exercise token cleanup.
    _mint_token(client, wid)
    r = client.delete(f"{PREFIX}/workflows/{wid}/service")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "unpublished"
    # Subsequent GET is 404.
    r = client.get(f"{PREFIX}/workflows/{wid}/service")
    assert r.status_code == 404
    # Tokens are gone too (admin list returns empty).
    r = client.get(f"{PREFIX}/workflows/{wid}/service/tokens")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []
