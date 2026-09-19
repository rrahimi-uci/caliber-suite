"""Route-level tests for operator-triggered webhook-delivery reconciliation
(`P4-E` slice 3): ``POST /projects/{id}/source/connection:reconcile-deliveries``.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkspaceSourceEvent
from caliber.github_source_control import GitHubResponse
from caliber.secret_store import SecretCipher, SecretStore, generate_key
from caliber.secrets import bind_secret_store, unbind_secret_store

PREFIX = "/ajax-api/2.0/mlflow/caliber"
WEBHOOK_SECRET = "wh-secret-value"


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    # Reconciliation mints a real GitHub App JWT (RS256), so route tests
    # exercising the happy path need a real key pair -- a placeholder
    # string would fail JWT signing before ever reaching the fake
    # transport, same requirement as test_github_webhook_reconciliation.py.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@dataclass
class _Transport:
    responses: dict[tuple[str, str], list[GitHubResponse]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method, path, *, headers, params=None, json_body=None):
        self.calls.append((method, path))
        queue = self.responses.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected request: {method} {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def close(self) -> None:
        # A real HTTPXGitHubTransport is closeable; the route must close
        # whatever transport it was handed, real or fake, once it's done.
        self.closed = True


def _delivery_entry(
    *, delivery_id: int, guid: str, installation_id: int = 1001
) -> dict[str, object]:
    return {
        "id": delivery_id,
        "guid": guid,
        "event": "pull_request",
        "action": "opened",
        "delivered_at": "2026-09-19T00:00:00Z",
        "status": "OK",
        "installation_id": installation_id,
    }


def _create_project(client: TestClient, name: str = "Reconciliation Route") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _configure_source(client: TestClient, project_id: str) -> str:
    response = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        json={
            "provider": "github",
            "provider_host": "github.com",
            "canonical_repository_id": "github:owner/repo",
            "display_path": "owner/repo",
            "default_branch": "main",
            "root_path": "",
            "manifest_path": ".caliber/workspace.yaml",
            "import_mode": "push",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["source"]["source_id"]


def _configure_connection(
    client: TestClient, project_id: str, *, installation_id: str = "1001", private_key: str
) -> None:
    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json={
            "provider": "github",
            "app_id": "app-1",
            "installation_id": installation_id,
            "private_key": private_key,
            "webhook_secret": WEBHOOK_SECRET,
        },
    )
    assert response.status_code == 201, response.text


@pytest.fixture
def bound_secret_store(
    client: TestClient, session_factory: sessionmaker[Session]
) -> Iterator[SecretStore]:
    store = SecretStore(SecretCipher(primary=base64.b64decode(generate_key())))
    client.app.state.secret_store = store
    bind_secret_store(store, session_factory)
    yield store
    unbind_secret_store()
    client.app.state.secret_store = None


@pytest.fixture
def fake_github_transport(client: TestClient) -> Iterator[_Transport]:
    transport = _Transport()
    client.app.state.github_transport_factory = lambda host: transport
    yield transport
    client.app.state.github_transport_factory = None


def test_reconcile_finds_and_requests_redelivery_of_a_missed_delivery(
    client: TestClient,
    bound_secret_store: SecretStore,
    fake_github_transport: _Transport,
    session_factory: sessionmaker[Session],
    rsa_private_key_pem: str,
) -> None:
    project_id = _create_project(client)
    source_id = _configure_source(client, project_id)
    _configure_connection(
        client, project_id, installation_id="1001", private_key=rsa_private_key_pem
    )
    fake_github_transport.responses[("GET", "/app/hooks/deliveries")] = [
        GitHubResponse(
            status_code=200,
            payload=[_delivery_entry(delivery_id=1, guid="missed-one", installation_id=1001)],
        )
    ]
    fake_github_transport.responses[("POST", "/app/hooks/deliveries/1/attempts")] = [
        GitHubResponse(status_code=202, payload={})
    ]

    response = client.post(f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["checked"] == 1
    assert data["missed_delivery_ids"] == ["missed-one"]
    assert data["redelivery_requested_ids"] == ["missed-one"]
    # Reconciliation itself never inserts into the inbox -- only a real
    # redelivered webhook POST would.
    with session_factory() as session:
        events = (
            session.execute(
                select(CaliberWorkspaceSourceEvent).where(
                    CaliberWorkspaceSourceEvent.source_id == source_id
                )
            )
            .scalars()
            .all()
        )
    assert events == []
    assert fake_github_transport.closed is True


def test_reconcile_reports_nothing_missed_once_converged(
    client: TestClient,
    bound_secret_store: SecretStore,
    fake_github_transport: _Transport,
    rsa_private_key_pem: str,
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)
    _configure_connection(
        client, project_id, installation_id="1001", private_key=rsa_private_key_pem
    )
    fake_github_transport.responses[("GET", "/app/hooks/deliveries")] = [
        GitHubResponse(status_code=200, payload=[])
    ]

    response = client.post(f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == {"checked": 0, "missed_delivery_ids": [], "redelivery_requested_ids": []}


def test_reconcile_without_a_connection_returns_409(
    client: TestClient, bound_secret_store: SecretStore
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)

    response = client.post(f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "workspace_source_connection_not_configured"


def test_reconcile_without_a_source_returns_404(
    client: TestClient, bound_secret_store: SecretStore
) -> None:
    project_id = _create_project(client)

    response = client.post(f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries")

    assert response.status_code == 404, response.text


def test_reconcile_requires_operator_scope(
    client: TestClient,
    bound_secret_store: SecretStore,
    fake_github_transport: _Transport,
    rsa_private_key_pem: str,
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)
    _configure_connection(
        client, project_id, installation_id="1001", private_key=rsa_private_key_pem
    )

    response = client.post(
        f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries",
        headers={"X-CALIBER-User": "@nobody"},
    )

    assert response.status_code in (401, 403), response.text


def test_reconcile_propagates_a_github_api_failure_as_502(
    client: TestClient,
    bound_secret_store: SecretStore,
    fake_github_transport: _Transport,
    rsa_private_key_pem: str,
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)
    _configure_connection(
        client, project_id, installation_id="1001", private_key=rsa_private_key_pem
    )
    fake_github_transport.responses[("GET", "/app/hooks/deliveries")] = [
        GitHubResponse(status_code=503, payload={})
    ]

    response = client.post(f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries")

    assert response.status_code == 502, response.text


def test_reconcile_tolerates_a_transport_with_no_close_method(
    client: TestClient,
    bound_secret_store: SecretStore,
    rsa_private_key_pem: str,
) -> None:
    """Not every injectable transport is closeable -- the route must not
    assume one is (``getattr``-guarded), only close it when it can."""

    class _NoCloseTransport:
        def request(self, method, path, *, headers, params=None, json_body=None):
            return GitHubResponse(status_code=200, payload=[])

    client.app.state.github_transport_factory = lambda host: _NoCloseTransport()
    try:
        project_id = _create_project(client)
        _configure_source(client, project_id)
        _configure_connection(
            client, project_id, installation_id="1001", private_key=rsa_private_key_pem
        )

        response = client.post(
            f"{PREFIX}/projects/{project_id}/source/connection:reconcile-deliveries"
        )
    finally:
        client.app.state.github_transport_factory = None

    assert response.status_code == 200, response.text
