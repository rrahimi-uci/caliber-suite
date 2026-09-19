"""Route-level contract tests for production GitHub webhook ingress (`P4-E` slice 2).

Proves the properties this task cares about most: a bad/missing signature
is refused and never reaches the durable inbox; a valid signature computed
with the *wrong* connection's secret is refused; every rejection reason is
indistinguishable from every other (no info leak that would help an
attacker enumerate configured installations); duplicate deliveries
converge through the existing inbox's idempotency without duplicate rows;
and the route is genuinely reachable without a CALIBER session/CSRF token/
rate-limit budget, since GitHub itself is the caller.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import CaliberWorkspaceSourceEvent
from caliber.routes.github_webhooks import PATH as WEBHOOK_PATH
from caliber.secret_store import SecretCipher, SecretStore, generate_key
from caliber.secrets import bind_secret_store, unbind_secret_store
from caliber.server import create_app

PREFIX = "/ajax-api/2.0/mlflow/caliber"
WEBHOOK_SECRET = "wh-secret-value-do-not-log"
OTHER_WEBHOOK_SECRET = "a-different-projects-secret"
PRIVATE_KEY = "unused-for-webhook-verification"


def _create_project(client: TestClient, name: str = "Webhook Ingress") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _configure_source(client: TestClient, project_id: str, *, repo: str) -> str:
    response = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        json={
            "provider": "github",
            "provider_host": "github.com",
            "canonical_repository_id": f"github:{repo}",
            "display_path": repo,
            "default_branch": "main",
            "root_path": "",
            "manifest_path": ".caliber/workspace.yaml",
            "import_mode": "push",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["source"]["source_id"]


def _configure_connection(
    client: TestClient,
    project_id: str,
    *,
    installation_id: str,
    webhook_secret: str = WEBHOOK_SECRET,
) -> None:
    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json={
            "provider": "github",
            "app_id": "app-1",
            "installation_id": installation_id,
            "private_key": PRIVATE_KEY,
            "webhook_secret": webhook_secret,
        },
    )
    assert response.status_code == 201, response.text


def _payload(*, repo: str, installation_id: str, action: str = "opened") -> bytes:
    return json.dumps(
        {
            "action": action,
            "installation": {"id": int(installation_id)},
            "repository": {"full_name": repo},
        }
    ).encode()


def _headers(
    payload: bytes,
    *,
    secret: str = WEBHOOK_SECRET,
    delivery: str = "delivery-1",
    event: str = "pull_request",
) -> dict[str, str]:
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def bound_secret_store(
    client: TestClient, session_factory: sessionmaker[Session]
) -> Iterator[SecretStore]:
    """Bind a real encrypted secret store onto the already-built test app.

    Mirrors ``test_workspace_source_connection_routes.py``'s fixture of the
    same name -- the webhook route needs a bound store to resolve any
    connection's secret, exactly like the connection-configure route does.
    """
    store = SecretStore(SecretCipher(primary=base64.b64decode(generate_key())))
    client.app.state.secret_store = store
    bind_secret_store(store, session_factory)
    yield store
    unbind_secret_store()
    client.app.state.secret_store = None


def _events(session_factory: sessionmaker[Session]) -> list[CaliberWorkspaceSourceEvent]:
    with session_factory() as session:
        return list(session.execute(select(CaliberWorkspaceSourceEvent)).scalars().all())


def test_valid_signature_is_accepted_and_recorded_in_the_durable_inbox(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    project_id = _create_project(client)
    source_id = _configure_source(client, project_id, repo="owner/repo")
    _configure_connection(client, project_id, installation_id="1001")
    payload = _payload(repo="owner/repo", installation_id="1001")

    response = client.post(WEBHOOK_PATH, content=payload, headers=_headers(payload))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["data"]["received"] is True
    assert body["data"]["duplicate"] is False
    events = _events(session_factory)
    assert len(events) == 1
    assert events[0].source_id == source_id
    assert events[0].provider_delivery_id == "delivery-1"
    assert events[0].status == "received"


def test_duplicate_delivery_is_idempotent_with_no_duplicate_row(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id, repo="owner/repo")
    _configure_connection(client, project_id, installation_id="1001")
    payload = _payload(repo="owner/repo", installation_id="1001")
    headers = _headers(payload)

    first = client.post(WEBHOOK_PATH, content=payload, headers=headers)
    second = client.post(WEBHOOK_PATH, content=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"]["duplicate"] is False
    assert second.json()["data"]["duplicate"] is True
    assert len(_events(session_factory)) == 1


def test_bad_signature_is_rejected_and_never_reaches_the_inbox(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id, repo="owner/repo")
    _configure_connection(client, project_id, installation_id="1001")
    payload = _payload(repo="owner/repo", installation_id="1001")
    bad_headers = _headers(payload, secret="totally-wrong-secret")

    response = client.post(WEBHOOK_PATH, content=payload, headers=bad_headers)

    assert response.status_code == 401
    assert _events(session_factory) == []


def test_unknown_installation_id_is_rejected_identically_to_a_bad_signature(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    """No info leak: 'no such installation' and 'wrong secret' must look the same."""
    project_id = _create_project(client)
    _configure_source(client, project_id, repo="owner/repo")
    _configure_connection(client, project_id, installation_id="1001")
    signed_payload = _payload(repo="owner/repo", installation_id="1001")
    bad_signature_response = client.post(
        WEBHOOK_PATH,
        content=signed_payload,
        headers=_headers(signed_payload, secret="totally-wrong-secret"),
    )

    unknown_payload = _payload(repo="owner/other-repo", installation_id="9999999")
    unknown_installation_response = client.post(
        WEBHOOK_PATH, content=unknown_payload, headers=_headers(unknown_payload)
    )

    assert bad_signature_response.status_code == unknown_installation_response.status_code == 401
    assert bad_signature_response.json() == unknown_installation_response.json()
    assert _events(session_factory) == []


def test_signature_valid_for_a_different_connections_secret_is_rejected(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    """The core collision scenario: an attacker (or misrouted client) signs
    with project B's secret but targets project A's installation id."""
    project_a = _create_project(client, "Project A")
    _configure_source(client, project_a, repo="owner/repo-a")
    _configure_connection(client, project_a, installation_id="1001", webhook_secret=WEBHOOK_SECRET)

    project_b = _create_project(client, "Project B")
    _configure_source(client, project_b, repo="owner/repo-b")
    _configure_connection(
        client, project_b, installation_id="2002", webhook_secret=OTHER_WEBHOOK_SECRET
    )

    # installation_id 1001 resolves to project A's connection, but the
    # payload is signed with project B's secret.
    payload = _payload(repo="owner/repo-a", installation_id="1001")
    headers = _headers(payload, secret=OTHER_WEBHOOK_SECRET)

    response = client.post(WEBHOOK_PATH, content=payload, headers=headers)

    assert response.status_code == 401
    assert _events(session_factory) == []


def test_malformed_json_body_is_rejected_generically(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        WEBHOOK_PATH,
        content=b"not valid json at all",
        headers={
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert _events(session_factory) == []


def test_missing_installation_object_is_rejected_generically(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    payload = json.dumps({"repository": {"full_name": "owner/repo"}}).encode()

    response = client.post(WEBHOOK_PATH, content=payload, headers=_headers(payload))

    assert response.status_code == 401
    assert _events(session_factory) == []


def test_payload_that_is_not_a_json_object_is_rejected_generically(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    payload = json.dumps(["not", "an", "object"]).encode()

    response = client.post(WEBHOOK_PATH, content=payload, headers=_headers(payload))

    assert response.status_code == 401
    assert _events(session_factory) == []


def test_installation_object_without_an_id_is_rejected_generically(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    payload = json.dumps({"installation": {}, "repository": {"full_name": "owner/repo"}}).encode()

    response = client.post(WEBHOOK_PATH, content=payload, headers=_headers(payload))

    assert response.status_code == 401
    assert _events(session_factory) == []


def test_reused_delivery_id_with_different_evidence_is_rejected_and_not_recorded(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    """The same ``X-GitHub-Delivery`` id arriving twice with genuinely
    different (independently signature-verified) content is a delivery-
    identity conflict, not a duplicate -- ``record_source_event`` refuses
    it rather than silently overwriting the first row."""
    project_id = _create_project(client)
    _configure_source(client, project_id, repo="owner/repo")
    _configure_connection(client, project_id, installation_id="1001")
    first_payload = _payload(repo="owner/repo", installation_id="1001", action="opened")
    second_payload = _payload(repo="owner/repo", installation_id="1001", action="closed")

    first = client.post(
        WEBHOOK_PATH, content=first_payload, headers=_headers(first_payload, delivery="dup-1")
    )
    second = client.post(
        WEBHOOK_PATH, content=second_payload, headers=_headers(second_payload, delivery="dup-1")
    )

    assert first.status_code == 202
    assert second.status_code == 401
    events = _events(session_factory)
    assert len(events) == 1
    assert events[0].provider_delivery_id == "dup-1"


def test_no_secret_store_bound_rejects_generically(client: TestClient) -> None:
    """The default ``client`` fixture has no secret store bound (production
    default). Every webhook must fail closed, not 500."""
    payload = _payload(repo="owner/repo", installation_id="1001")

    response = client.post(WEBHOOK_PATH, content=payload, headers=_headers(payload))

    assert response.status_code == 401


def test_body_exceeding_the_configured_ceiling_returns_413(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "caliber-webhook-size.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("CALIBER_ADMIN_USERS", "@admin")
    monkeypatch.setenv("CALIBER_GITHUB_WEBHOOK_MAX_BODY_BYTES", "16")
    config = CaliberConfig.load()
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as oversized_client:
        response = oversized_client.post(
            WEBHOOK_PATH,
            content=b"x" * 1024,
            headers={
                "X-GitHub-Delivery": "delivery-1",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 413


def test_secret_material_never_appears_in_any_response(
    client: TestClient, bound_secret_store: SecretStore, session_factory: sessionmaker[Session]
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id, repo="owner/repo")
    _configure_connection(client, project_id, installation_id="1001")
    payload = _payload(repo="owner/repo", installation_id="1001")

    accepted = client.post(WEBHOOK_PATH, content=payload, headers=_headers(payload))
    rejected = client.post(
        WEBHOOK_PATH, content=payload, headers=_headers(payload, secret="wrong-secret")
    )

    for response in (accepted, rejected):
        assert WEBHOOK_SECRET not in response.text
        assert PRIVATE_KEY not in response.text


def _build_csrf_and_rate_limited_client(
    *,
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """A live app with CSRF *and* rate limiting both genuinely enabled.

    Proves the webhook route's exemption wiring in server.py actually
    works end to end -- an unauthenticated POST with no CSRF token and no
    CALIBER identity must still reach the handler, not be rejected by
    either middleware before it ever runs.
    """
    db_path = tmp_path / "caliber-webhook-csrf.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("CALIBER_ADMIN_USERS", "@admin")
    monkeypatch.setenv("CALIBER_CSRF_ENABLED", "true")
    monkeypatch.setenv("CALIBER_CSRF_SIGNING_SECRET_ENV", "CSRF_TEST_KEY")
    monkeypatch.setenv("CSRF_TEST_KEY", "csrf-test-secret")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_BURST", "1")
    config = CaliberConfig.load()
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    return TestClient(app)


def test_webhook_route_is_exempt_from_csrf_and_rate_limiting(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _build_csrf_and_rate_limited_client(
        tmp_path=tmp_path, engine=engine, session_factory=session_factory, monkeypatch=monkeypatch
    )
    with client:
        payload = _payload(repo="owner/repo", installation_id="1001")
        headers = _headers(payload)

        # No X-CALIBER-CSRF header, no X-CALIBER-User, and a rate-limit
        # budget of 1/minute exhausted by firing several requests -- if the
        # route were not exempt from either middleware this would 403 or
        # 429 before ever reaching the handler. It must reach the handler
        # and fail closed on "no connection configured" (401) instead.
        statuses = {
            client.post(WEBHOOK_PATH, content=payload, headers=headers).status_code
            for _ in range(5)
        }

    assert statuses == {401}
