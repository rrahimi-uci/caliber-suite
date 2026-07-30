"""Integration tests for the deploy-workflow-as-a-service routes."""

from __future__ import annotations

import pytest
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


def _publish_service(client: TestClient, *, auth_required: bool | None = None) -> tuple[str, str]:
    """Create+publish a workflow, deploy it to prod, then publish the service.

    New services require a token by default. Pass ``auth_required=False`` only
    for tests that deliberately exercise the explicit public-service escape
    hatch. Returns ``(workflow_id, version_id)``.
    """
    wid, vid = create_and_publish(client)
    deploy_prod(client, wid, vid)
    body = {} if auth_required is None else {"auth_required": auth_required}
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
    assert data["auth_required"] is True
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


def test_internal_service_routes_hide_an_out_of_scope_workflow(client: TestClient) -> None:
    """Operator scope alone must not unlock another owner's nested service."""
    wid, _ = _publish_service(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test", "operator_users": "@operator"}
    )
    headers = {"X-CALIBER-User": "@operator"}

    assert client.get(f"{PREFIX}/workflows/{wid}/service", headers=headers).status_code == 404
    assert (
        client.post(
            f"{PREFIX}/workflows/{wid}/service",
            json={"auth_required": True},
            headers=headers,
        ).status_code
        == 404
    )
    assert client.delete(f"{PREFIX}/workflows/{wid}/service", headers=headers).status_code == 404
    assert (
        client.post(
            f"{PREFIX}/workflows/{wid}/service/tokens",
            json={"name": "hidden"},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.get(f"{PREFIX}/workflows/{wid}/service/tokens", headers=headers).status_code == 404
    )
    assert (
        client.get(
            f"{PREFIX}/workflows/{wid}/service/openapi.json",
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"{PREFIX}/workflows/{wid}/service/tokens/not-mine",
            headers=headers,
        ).status_code
        == 404
    )


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
    # Public access remains possible only when the publisher explicitly opts out.
    wid, _ = _publish_service(client, auth_required=False)
    r = client.post(f"{PREFIX}/services/{wid}/invoke", json={"input": {"user_message": "hi"}})
    assert r.status_code == 202, r.text
    data = r.json()["data"]
    assert data["status"] == "queued"
    assert data["run_id"]


def test_invoke_writes_audit_row(client: TestClient, db_session: Session) -> None:
    wid, _ = _publish_service(client, auth_required=False)
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
    assert rows[0].actor == f"anonymous_service:{wid}"


def test_invoke_with_invalid_input_is_400(client: TestClient) -> None:
    # Input is validated regardless of auth. user_message is a typed string in the
    # manifest, so a number fails validation.
    wid, _ = _publish_service(client, auth_required=False)
    r = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": 123}},
    )
    assert r.status_code == 400, r.text


def test_get_run_status_open(client: TestClient) -> None:
    wid, _ = _publish_service(client, auth_required=False)
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


def test_auth_required_enforces_bearer_token(client: TestClient, db_session: Session) -> None:
    # The default publish path is protected without an opt-in flag.
    wid, _ = _publish_service(client)

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
    invoke_audit = (
        db_session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.entity_id == wid)
            .where(CaliberAuditLog.action == "invoke_workflow_service")
        )
        .scalars()
        .one()
    )
    assert invoke_audit.actor.startswith("service_token:")
    assert wid not in invoke_audit.actor

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
    wid, _ = _publish_service(client, auth_required=False)
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


def test_internal_openapi_uses_workflow_auth_without_weakening_external_auth(
    client: TestClient,
) -> None:
    wid, _ = _publish_service(client)

    # Studio can download the raw document through normal CALIBER auth even
    # though the published service itself is token protected.
    internal = client.get(f"{PREFIX}/workflows/{wid}/service/openapi.json")
    assert internal.status_code == 200, internal.text
    internal_spec = internal.json()
    assert "data" not in internal_spec
    assert internal_spec["components"]["securitySchemes"]["serviceToken"] == {
        "type": "http",
        "scheme": "bearer",
    }

    # The public-facing document keeps the same Bearer requirement and is
    # generated from the same canonical builder.
    external_path = f"{PREFIX}/services/{wid}/openapi.json"
    assert client.get(external_path).status_code == 401
    token = _mint_token(client, wid)
    external = client.get(
        external_path,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert external.status_code == 200, external.text
    assert external.json() == internal_spec


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


# ---------------------------------------------------------------------------
# Published-service quotas and CORS
# ---------------------------------------------------------------------------


def test_a_service_rate_limit_refuses_excess_traffic_with_retry_after(
    client: TestClient,
) -> None:
    """A published service was authenticated but otherwise unbounded, so any token
    holder could drive unlimited traffic through a workflow that calls paid model APIs.

    429 with ``Retry-After`` rather than 400: this is a temporary condition, and saying
    so is the difference between a client backing off and a client hammering.
    """
    wid, _vid = _publish_service(client)
    patched = client.post(f"{PREFIX}/workflows/{wid}/service", json={"rate_limit_per_minute": 2})
    assert patched.status_code == 200, patched.text
    token = _mint_token(client, wid)
    headers = {"Authorization": f"Bearer {token}"}

    accepted = [
        client.post(
            f"{PREFIX}/services/{wid}/invoke",
            json={"input": {"user_message": "hi"}},
            headers=headers,
        )
        for _ in range(2)
    ]
    assert all(r.status_code == 202 for r in accepted), [r.status_code for r in accepted]

    refused = client.post(
        f"{PREFIX}/services/{wid}/invoke", json={"input": {"user_message": "hi"}}, headers=headers
    )
    assert refused.status_code == 429, refused.text
    assert "rate limit" in refused.json()["detail"]
    assert int(refused.headers["Retry-After"]) >= 1


def test_the_default_service_has_no_rate_limit(client: TestClient) -> None:
    """0 means unlimited and is the default, so an upgrade does not start refusing
    traffic for services that worked yesterday. The ceiling is opt-in."""
    wid, _vid = _publish_service(client)
    body = client.get(f"{PREFIX}/workflows/{wid}/service").json()["data"]
    assert body["rate_limit_per_minute"] == 0
    assert body["cors_allowed_origins"] == ""


def test_cors_headers_are_emitted_only_for_a_listed_origin(client: TestClient) -> None:
    """Unset must mean *no* browser access, not all of it: a wildcard would let any
    site read a token-authorized response."""
    wid, _vid = _publish_service(client)
    token = _mint_token(client, wid)
    auth = {"Authorization": f"Bearer {token}"}

    # Nothing configured -> no CORS headers at all.
    plain = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers={**auth, "Origin": "https://evil.example"},
    )
    assert plain.status_code == 202, plain.text
    assert "Access-Control-Allow-Origin" not in plain.headers

    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example,https://admin.example"},
    )
    allowed = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers={**auth, "Origin": "https://app.example"},
    )
    assert allowed.headers.get("Access-Control-Allow-Origin") == "https://app.example"
    assert allowed.headers.get("Vary") == "Origin"

    # An unlisted origin is not reflected back.
    denied = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers={**auth, "Origin": "https://evil.example"},
    )
    assert "Access-Control-Allow-Origin" not in denied.headers


def test_a_browser_preflight_is_answered_for_a_listed_origin(client: TestClient) -> None:
    """Without an OPTIONS handler the CORS allowlist was decorative.

    A browser sending `Authorization: Bearer …` with a JSON content type must preflight
    first. The route accepted only POST, so the preflight got 405 and the real request
    was never sent — allow-origin headers on the POST response cannot help when the
    browser never reaches it. An independent probe reproduced the 405.
    """
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )

    response = client.options(
        f"{PREFIX}/services/{wid}/invoke",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 204, response.text
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    # The requested headers are echoed rather than guessed at.
    assert "authorization" in response.headers["Access-Control-Allow-Headers"].lower()


def test_a_preflight_from_an_unlisted_origin_gets_no_allow_headers(
    client: TestClient,
) -> None:
    """The preflight answers without authentication (a preflight carries no
    credentials), so it must not become a way to reach a service from anywhere."""
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )

    response = client.options(
        f"{PREFIX}/services/{wid}/invoke",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 204
    assert "Access-Control-Allow-Origin" not in response.headers


def test_a_preflight_for_an_unknown_service_discloses_nothing(client: TestClient) -> None:
    """An unauthenticated caller must not learn which workflow ids have services."""
    response = client.options(
        f"{PREFIX}/services/WF-does-not-exist/invoke",
        headers={"Origin": "https://app.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 204
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("suffix", ["runs/RUN-1", "openapi.json"])
def test_the_poll_and_spec_endpoints_answer_a_browser_preflight(
    client: TestClient, suffix: str
) -> None:
    """Preflighting only ``/invoke`` left the CORS support half-built.

    ``Authorization`` is not a CORS-safelisted request header, so a bearer-token ``GET``
    is preflighted exactly like the ``POST``. With OPTIONS registered on invoke alone, a
    browser could start a run and then be blocked from polling for its result — which is
    not a usable service, only one that fails later.
    """
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )

    response = client.options(
        f"{PREFIX}/services/{wid}/{suffix}",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 204, response.text
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example"
    # A read path must advertise GET, not the invoke path's POST.
    assert response.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"


def test_a_preflight_advertises_only_the_method_its_path_accepts(
    client: TestClient,
) -> None:
    """A blanket allow-methods list would tell a browser it may POST to the read-only
    poll endpoint, and the browser would believe it right up to the 405."""
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )
    headers = {"Origin": "https://app.example", "Access-Control-Request-Method": "POST"}

    invoke = client.options(f"{PREFIX}/services/{wid}/invoke", headers=headers)
    poll = client.options(f"{PREFIX}/services/{wid}/runs/RUN-1", headers=headers)

    assert invoke.headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
    assert "POST" not in poll.headers["Access-Control-Allow-Methods"]


def test_the_spec_response_carries_the_allow_origin_header(client: TestClient) -> None:
    """A passing preflight is not enough: the browser also enforces the header on the
    actual response, so a spec fetch without it is still blocked."""
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )
    token = _mint_token(client, wid)

    allowed = client.get(
        f"{PREFIX}/services/{wid}/openapi.json",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://app.example"},
    )
    denied = client.get(
        f"{PREFIX}/services/{wid}/openapi.json",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )

    assert allowed.status_code == 200, allowed.text
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://app.example"
    assert "Access-Control-Allow-Origin" not in denied.headers


def test_the_poll_response_carries_the_allow_origin_header(client: TestClient) -> None:
    """The run-status read is the one a browser polls in a loop; without the header the
    caller can start work and never learn the outcome."""
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )
    token = _mint_token(client, wid)
    auth = {"Authorization": f"Bearer {token}"}

    started = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers=auth,
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["data"]["run_id"]

    polled = client.get(
        f"{PREFIX}/services/{wid}/runs/{run_id}",
        headers={**auth, "Origin": "https://app.example"},
    )

    assert polled.status_code == 200, polled.text
    assert polled.headers["Access-Control-Allow-Origin"] == "https://app.example"


def test_error_responses_also_carry_the_allow_origin_header(client: TestClient) -> None:
    """A browser must be able to read *why* a call failed, not only a success.

    Found by an independent probe. Failures are raised as `HTTPException` and rendered by
    the global handler, which knows nothing about a per-service origin allowlist — so the
    route-local headers only ever covered the happy path. The 429 arrived with a correct
    `Retry-After` and no `Access-Control-Allow-Origin`, leaving the documented error
    contract unreadable to JavaScript on an approved origin.
    """
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example", "rate_limit_per_minute": 1},
    )
    token = _mint_token(client, wid)
    auth = {"Authorization": f"Bearer {token}", "Origin": "https://app.example"}
    body = {"input": {"user_message": "hi"}}

    client.post(f"{PREFIX}/services/{wid}/invoke", json=body, headers=auth)
    throttled = client.post(f"{PREFIX}/services/{wid}/invoke", json=body, headers=auth)

    assert throttled.status_code == 429, throttled.text
    assert throttled.headers["Retry-After"]
    assert throttled.headers["Access-Control-Allow-Origin"] == "https://app.example"

    # Still an allowlist, not a wildcard: an unlisted origin gets nothing on errors either.
    denied = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )
    assert "Access-Control-Allow-Origin" not in denied.headers


def test_an_unauthenticated_error_also_carries_the_allow_origin_header(
    client: TestClient,
) -> None:
    """The 401 path matters most: it is the first error a misconfigured browser client
    hits, and an opaque one gives the developer nothing to debug with."""
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )

    response = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers={"Authorization": "Bearer not-a-real-token", "Origin": "https://app.example"},
    )

    assert response.status_code == 401, response.text
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example"


def test_request_validation_errors_also_carry_the_allow_origin_header(
    client: TestClient,
) -> None:
    """Pydantic validation is handled separately from ``HTTPException``.

    The route wrapper originally caught only ``HTTPException``. An extra request field
    raises ``pydantic.ValidationError`` instead, so Starlette sent it through the global
    400 handler after the wrapper had already been bypassed. The browser then received a
    valid structured error that JavaScript on an approved origin was forbidden to read.
    Error-path CORS is complete only if both exception families preserve the same origin
    policy and the existing validation-error body.
    """
    wid, _vid = _publish_service(client)
    client.post(
        f"{PREFIX}/workflows/{wid}/service",
        json={"cors_allowed_origins": "https://app.example"},
    )
    token = _mint_token(client, wid)

    response = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}, "unexpected": True},
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://app.example",
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "request body validation failed"
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example"


def test_invalid_tokens_do_not_consume_the_service_budget(
    client: TestClient,
) -> None:
    """The budget is charged after authentication, so junk traffic cannot spend it.

    It was originally charged before, to stop a flood of invalid tokens. A review pass
    pointed out that the flood then denies legitimate callers instead — and separate
    buckets do not help, because before authenticating you cannot tell the two apart.
    Flood protection is ``RateLimitMiddleware``'s job, applied per caller; this limit is
    the per-service work budget, so only real invocations should charge it.
    """
    wid, _vid = _publish_service(client)
    client.post(f"{PREFIX}/workflows/{wid}/service", json={"rate_limit_per_minute": 2})
    token = _mint_token(client, wid)

    # Exhaust the *unauthenticated* bucket with bad tokens.
    for _ in range(3):
        client.post(
            f"{PREFIX}/services/{wid}/invoke",
            json={"input": {"user_message": "hi"}},
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    # A valid caller must still get its own full allowance.
    accepted = client.post(
        f"{PREFIX}/services/{wid}/invoke",
        json={"input": {"user_message": "hi"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert accepted.status_code == 202, accepted.text
