"""Integration tests for ``/caliber/system/services`` (platform health).

HTTP + TCP probes are monkeypatched; the DB probe runs for real against the
test engine (SELECT 1 on the fixture SQLite), exercising that path end-to-end.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import caliber.routes.system_services as svc

SERVICES = "/ajax-api/2.0/mlflow/caliber/system/services"


def _set_config(client: TestClient, **updates: object) -> None:
    client.app.state.config = client.app.state.config.model_copy(update=updates)


def _patch_probes(monkeypatch: pytest.MonkeyPatch, *, http: svc._Probe, tcp: svc._Probe) -> None:
    async def _http(*_a: object, **_k: object) -> svc._Probe:
        return http

    async def _tcp(*_a: object, **_k: object) -> svc._Probe:
        return tcp

    monkeypatch.setattr(svc, "_probe_http", _http)
    monkeypatch.setattr(svc, "_probe_tcp", _tcp)


def test_lists_services_with_health(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(
        client,
        gateway_uri="http://gw:5002",
        knowledge_age_viewer_url="http://localhost:8082",
    )
    _patch_probes(
        monkeypatch,
        http=svc._Probe(True, "HTTP 200", 10),
        tcp=svc._Probe(True, "TCP open", 4),
    )

    resp = client.get(SERVICES)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "checked_at_ms" in data
    by_key = {s["key"]: s for s in data["services"]}

    # Core services always present; graph console appears because age_url is set.
    assert {
        "mlflow",
        "mlflow_gateway",
        "object_store",
        "database",
        "event_bus",
        "graph_console",
    } <= set(by_key)
    assert by_key["mlflow"]["healthy"] is True
    assert by_key["mlflow"]["url"].endswith(":5000")
    assert by_key["event_bus"]["healthy"] is True
    # The DB probe ran for real against the fixture engine.
    assert by_key["database"]["healthy"] is True
    assert "SELECT 1 ok" in by_key["database"]["detail"]
    # Non-HTTP services expose no browse URL.
    assert by_key["event_bus"]["url"] is None


def test_marks_down_service(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(
        monkeypatch,
        http=svc._Probe(False, "connection refused", None),
        tcp=svc._Probe(False, "connection refused", None),
    )
    resp = client.get(SERVICES)
    assert resp.status_code == 200, resp.text
    by_key = {s["key"]: s for s in resp.json()["data"]["services"]}
    assert by_key["mlflow"]["healthy"] is False
    assert "connection refused" in by_key["mlflow"]["detail"]
    assert by_key["mlflow"]["latency_ms"] is None


def test_graph_console_probes_internal_address(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docker-localhost trap: the public :8082 browse URL is unreachable from
    the CALIBER container, but the internal age-viewer:3000 address is up — the
    graph console must still report healthy (regression: showed "down")."""
    _set_config(client, knowledge_age_viewer_url="http://localhost:8082")

    async def _http(_c: object, url: str) -> svc._Probe:
        if ":8082" in url:  # public host — not reachable from inside the container
            return svc._Probe(False, "connection refused", None)
        return svc._Probe(True, "HTTP 200", 5)

    async def _tcp(*_a: object, **_k: object) -> svc._Probe:
        return svc._Probe(True, "TCP open", 2)

    monkeypatch.setattr(svc, "_probe_http", _http)
    monkeypatch.setattr(svc, "_probe_tcp", _tcp)

    by_key = {s["key"]: s for s in client.get(SERVICES).json()["data"]["services"]}
    assert by_key["graph_console"]["healthy"] is True
    # Browse link stays the public URL the user clicks.
    assert by_key["graph_console"]["url"] == "http://localhost:8082"


def test_graph_console_omitted_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(client, knowledge_age_viewer_url=None)
    _patch_probes(
        monkeypatch,
        http=svc._Probe(True, "HTTP 200", 1),
        tcp=svc._Probe(True, "TCP open", 1),
    )
    by_key = {s["key"]: s for s in client.get(SERVICES).json()["data"]["services"]}
    assert "graph_console" not in by_key
