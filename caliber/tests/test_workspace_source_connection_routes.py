"""Route-level contract tests for GitHub App connection storage (`P4-E`).

Companion to ``test_workspace_routes.py``'s P4-C source lifecycle tests,
covering the new ``/projects/{id}/source/connection`` surface: permission
gating identical to the source routes, secret-free responses (the private
key and webhook secret must never appear in any response body), and correct
409/503/404 failure states.
"""

from __future__ import annotations

import base64

import pytest
from starlette.testclient import TestClient

from caliber.secret_store import SecretCipher, generate_key
from caliber.secrets import bind_secret_store, unbind_secret_store

PREFIX = "/ajax-api/2.0/mlflow/caliber"
PRIVATE_KEY_VALUE = (
    "-----BEGIN RSA PRIVATE KEY-----\nfake-key-material\n-----END RSA PRIVATE KEY-----\n"
)
WEBHOOK_SECRET_VALUE = "wh-super-secret-value"


def _create_project(client: TestClient, name: str = "Connection Routes") -> str:
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


def _connection_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": "github",
        "app_id": "123456",
        "installation_id": "987654",
        "private_key": PRIVATE_KEY_VALUE,
        "webhook_secret": WEBHOOK_SECRET_VALUE,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def bound_secret_store(client: TestClient, session_factory):  # type: ignore[no-untyped-def]
    """Bind a real encrypted secret store onto the already-built test app.

    The ``client`` fixture builds ``app.state`` without a secret store (no
    ``CALIBER_SECRET_ENCRYPTION_KEY_SOURCE`` is set for the default test
    config), matching production's "off unless configured" default. The
    connection routes need one bound to do anything, so tests that exercise
    them opt in explicitly here -- mirroring
    ``test_secret_store.py``'s ``bound_store`` fixture.
    """
    from caliber.secret_store import SecretStore

    store = SecretStore(SecretCipher(primary=base64.b64decode(generate_key())))
    client.app.state.secret_store = store
    bind_secret_store(store, session_factory)
    yield store
    unbind_secret_store()
    client.app.state.secret_store = None


def test_configure_connection_response_never_contains_secret_material(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)

    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json=_connection_payload(),
    )

    assert response.status_code == 201, response.text
    body = response.text
    assert PRIVATE_KEY_VALUE not in body
    assert WEBHOOK_SECRET_VALUE not in body
    connection = response.json()["data"]["connection"]
    assert connection["app_id"] == "123456"
    assert connection["installation_id"] == "987654"
    assert connection["status"] == "active"
    assert "private_key" not in connection
    assert "webhook_secret" not in connection
    assert "private_key_ref" not in connection
    assert "webhook_secret_ref" not in connection


def test_get_connection_is_also_secret_free_and_reflects_configuration(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)
    client.put(f"{PREFIX}/projects/{project_id}/source/connection", json=_connection_payload())

    response = client.get(f"{PREFIX}/projects/{project_id}/source/connection")

    assert response.status_code == 200, response.text
    assert PRIVATE_KEY_VALUE not in response.text
    assert WEBHOOK_SECRET_VALUE not in response.text
    assert response.json()["data"]["connection"]["app_id"] == "123456"


def test_get_connection_before_configuration_returns_none(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)

    response = client.get(f"{PREFIX}/projects/{project_id}/source/connection")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["connection"] is None


def test_get_connection_without_any_source_configured_returns_none(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)

    response = client.get(f"{PREFIX}/projects/{project_id}/source/connection")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["connection"] is None


def test_configure_connection_rejects_a_source_provider_mismatch(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    response = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        json={
            "provider": "gitlab",
            "provider_host": "gitlab.com",
            "canonical_repository_id": "gitlab:owner/repo",
            "display_path": "owner/repo",
            "default_branch": "main",
            "root_path": "",
            "manifest_path": ".caliber/workspace.yaml",
            "import_mode": "push",
        },
    )
    assert response.status_code == 200, response.text

    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json=_connection_payload(),
    )

    assert response.status_code == 409, response.text
    assert "provider_mismatch" in response.json()["detail"]


def test_configure_connection_without_a_source_first_is_rejected(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)

    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json=_connection_payload(),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "workspace_source_not_configured"


def test_configure_connection_without_bound_secret_store_returns_503(client: TestClient) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)

    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json=_connection_payload(),
    )

    assert response.status_code == 503, response.text


def test_configure_connection_requires_operator_scope(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)

    response = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json=_connection_payload(),
        headers={"X-CALIBER-User": "@nobody"},
    )

    assert response.status_code in (401, 403), response.text


def test_revoke_connection_clears_it_and_is_idempotently_not_found_after(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)
    client.put(f"{PREFIX}/projects/{project_id}/source/connection", json=_connection_payload())

    revoked = client.post(f"{PREFIX}/projects/{project_id}/source/connection:revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["connection"]["status"] == "revoked"

    after = client.get(f"{PREFIX}/projects/{project_id}/source/connection")
    assert after.json()["data"]["connection"] is None

    again = client.post(f"{PREFIX}/projects/{project_id}/source/connection:revoke")
    assert again.status_code == 404, again.text


def test_revoke_connection_without_a_source_returns_404(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)

    response = client.post(f"{PREFIX}/projects/{project_id}/source/connection:revoke")

    assert response.status_code == 404, response.text


def test_configure_connection_rotates_and_source_has_connection_stays_true(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)
    first = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection", json=_connection_payload()
    )
    first_connection_id = first.json()["data"]["connection"]["connection_id"]

    second = client.put(
        f"{PREFIX}/projects/{project_id}/source/connection",
        json=_connection_payload(app_id="999999"),
    )

    assert second.status_code == 201, second.text
    assert second.json()["data"]["connection"]["connection_id"] == first_connection_id
    assert second.json()["data"]["connection"]["app_id"] == "999999"

    source = client.get(f"{PREFIX}/projects/{project_id}/source")
    assert source.json()["data"]["source"]["has_connection"] is True


def test_workspace_context_mismatch_is_rejected(
    client: TestClient, bound_secret_store: object
) -> None:
    project_id = _create_project(client)
    _configure_source(client, project_id)

    response = client.get(
        f"{PREFIX}/projects/{project_id}/source/connection",
        headers={"X-CALIBER-Project": "some-other-project"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "workspace_context_mismatch"
