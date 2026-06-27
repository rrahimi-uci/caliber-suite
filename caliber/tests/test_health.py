"""Health endpoint integration tests.

These tests prove the plugin shell loads cleanly and responds at the documented
URL with the documented payload shape. They are the smallest end-to-end CALIBER
test possible — feedback poller, refinement workers, and DB are all out of
scope here. As such, they're what we expect to stay green forever.
"""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

import caliber
from caliber.routes.health import HEALTH_PATH


def test_health_endpoint_returns_200(client: TestClient) -> None:
    response = client.get(HEALTH_PATH)
    assert response.status_code == 200


def test_health_endpoint_returns_json(client: TestClient) -> None:
    response = client.get(HEALTH_PATH)
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == caliber.__version__
    # The DB probe ran and reports healthy (the test DB is live).
    assert payload["db"] == "ok"


def test_health_reports_503_when_db_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomSession:
        def __enter__(self) -> _BoomSession:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, *_a: object, **_k: object) -> object:
            raise RuntimeError("database is down")

    monkeypatch.setattr(client.app.state, "session_factory", lambda: _BoomSession())
    response = client.get(HEALTH_PATH)
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["db"] == "down"
    assert payload["version"] == caliber.__version__


def test_health_endpoint_path_is_mlflow_namespaced() -> None:
    # The path is part of the public contract — it must live under
    # /ajax-api/2.0/mlflow/caliber/ so it is same-origin with MLflow's
    # own AJAX API. Pin it here so a refactor cannot silently break it.
    assert HEALTH_PATH == "/ajax-api/2.0/mlflow/caliber/health"


def test_unknown_endpoint_returns_404(client: TestClient) -> None:
    response = client.get("/ajax-api/2.0/mlflow/caliber/does-not-exist")
    assert response.status_code == 404


def test_mlflow_http_timeout_set_by_create_app(client: TestClient) -> None:
    """Phase 1 audit (#2): MLflow's HTTP client respects the
    ``MLFLOW_HTTP_REQUEST_TIMEOUT`` env var. Without a default, a
    hung MLflow tracking server would freeze the feedback poller
    indefinitely. ``create_app`` sets a sane default if the operator
    hasn't already chosen one."""
    _ = client  # ensures create_app has run by the time we check
    assert os.environ.get("MLFLOW_HTTP_REQUEST_TIMEOUT") is not None
