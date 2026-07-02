"""Coverage-focused tests for ``caliber.routes.system_services``.

The happy-path suite (``test_routes_system_services.py``) always monkeypatches
``_probe_http`` / ``_probe_tcp`` wholesale, so their real bodies (the actual
``httpx`` / ``asyncio.open_connection`` calls) never execute. This file drives
those probe helpers directly with ``asyncio.run(...)`` against a real (but
short-lived, loopback-only) TCP listener and a fake ``httpx.AsyncClient``
double, plus the small pure-function helpers (``_db_target``, ``_probe_db_sync``)
and the "unconfigured service" (``_noop_probe``) branch of the route itself.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from starlette.testclient import TestClient

import caliber.routes.system_services as svc

SERVICES = "/ajax-api/2.0/mlflow/caliber/system/services"


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAsyncClient:
    """A minimal double for ``httpx.AsyncClient`` — only ``get`` is awaited."""

    def __init__(self, *, status_code: int | None = None, exc: Exception | None = None) -> None:
        self._status_code = status_code
        self._exc = exc

    async def get(self, _url: str) -> _FakeResponse:
        if self._exc is not None:
            raise self._exc
        assert self._status_code is not None
        return _FakeResponse(self._status_code)


# ---------------------------------------------------------------------------
# _probe_http
# ---------------------------------------------------------------------------


def test_probe_http_success_reports_healthy() -> None:
    client = _FakeAsyncClient(status_code=200)
    result = asyncio.run(svc._probe_http(client, "http://svc/health"))
    assert result.healthy is True
    assert result.detail == "HTTP 200"
    assert result.latency_ms is not None


def test_probe_http_server_error_status_reports_unhealthy() -> None:
    client = _FakeAsyncClient(status_code=503)
    result = asyncio.run(svc._probe_http(client, "http://svc/health"))
    assert result.healthy is False
    assert result.detail == "HTTP 503"


def test_probe_http_connection_exception_reports_unhealthy() -> None:
    client = _FakeAsyncClient(exc=ConnectionRefusedError("connection refused"))
    result = asyncio.run(svc._probe_http(client, "http://svc/health"))
    assert result.healthy is False
    assert "connection refused" in result.detail
    assert result.latency_ms is None


# ---------------------------------------------------------------------------
# _probe_http_any
# ---------------------------------------------------------------------------


def test_probe_http_any_skips_empty_urls_and_returns_first_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_probe(_client: object, url: str) -> svc._Probe:
        calls.append(url)
        return svc._Probe(url == "http://good", "checked", 1)

    monkeypatch.setattr(svc, "_probe_http", fake_probe)

    result = asyncio.run(
        svc._probe_http_any(object(), ["", "http://bad1", "http://good", "http://bad2"])
    )
    assert result.healthy is True
    # The empty candidate is skipped; the loop stops at the first healthy URL.
    assert calls == ["http://bad1", "http://good"]


def test_probe_http_any_falls_through_when_all_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(_client: object, _url: str) -> svc._Probe:
        return svc._Probe(False, "down", None)

    monkeypatch.setattr(svc, "_probe_http", fake_probe)

    result = asyncio.run(svc._probe_http_any(object(), ["http://a", "http://b"]))
    assert result.healthy is False
    assert result.detail == "down"


def test_probe_http_any_no_candidates_returns_default() -> None:
    result = asyncio.run(svc._probe_http_any(object(), [""]))
    assert result.healthy is False
    assert result.detail == "no candidate URL"


# ---------------------------------------------------------------------------
# _probe_tcp — real loopback TCP connect / connection-refused
# ---------------------------------------------------------------------------


def test_probe_tcp_success_against_real_listener() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        result = asyncio.run(svc._probe_tcp("127.0.0.1", port))
        assert result.healthy is True
        assert f"TCP 127.0.0.1:{port} open" == result.detail
        assert result.latency_ms is not None
    finally:
        server.close()


def test_probe_tcp_failure_when_nothing_listening() -> None:
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()  # freed immediately — nothing is listening on it now

    result = asyncio.run(svc._probe_tcp("127.0.0.1", port))
    assert result.healthy is False
    assert result.latency_ms is None


# ---------------------------------------------------------------------------
# _probe_db_sync
# ---------------------------------------------------------------------------


def test_probe_db_sync_success() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    try:
        result = svc._probe_db_sync(engine)
        assert result.healthy is True
        assert result.detail == "SELECT 1 ok"
        assert result.latency_ms is not None
    finally:
        engine.dispose()


def test_probe_db_sync_failure() -> None:
    class _BadEngine:
        def connect(self) -> object:
            raise RuntimeError("db unreachable")

    result = svc._probe_db_sync(_BadEngine())
    assert result.healthy is False
    assert "db unreachable" in result.detail
    assert result.latency_ms is None


# ---------------------------------------------------------------------------
# _db_target
# ---------------------------------------------------------------------------


def test_db_target_sqlite() -> None:
    assert svc._db_target("sqlite:///./caliber.db") == "sqlite (local file)"


def test_db_target_network_database() -> None:
    assert svc._db_target("postgresql://user:pass@dbhost:5432/mydb") == "dbhost:5432/mydb"


def test_db_target_parse_failure_returns_generic_label() -> None:
    # ``urlparse`` raises AttributeError on a non-string, non-list-like input
    # (e.g. an int) — exercises the defensive except branch rather than ever
    # surfacing a 500 to the Settings page.
    assert svc._db_target(12345) == "database"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_services — the "unconfigured" branch (_noop_probe) for DB + event bus
# ---------------------------------------------------------------------------


def test_get_services_reports_not_configured_when_engine_and_nats_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No engine on app.state → the DB probe coroutine is None → _noop_probe.
    client.app.state.engine = None
    # A NATS URL with no parseable host → nats_host is None → _noop_probe.
    client.app.state.config = client.app.state.config.model_copy(update={"nats_url": "not-a-url"})

    async def _http(*_a: object, **_k: object) -> svc._Probe:
        return svc._Probe(False, "down", None)

    monkeypatch.setattr(svc, "_probe_http", _http)

    resp = client.get(SERVICES)
    assert resp.status_code == 200, resp.text
    by_key = {s["key"]: s for s in resp.json()["data"]["services"]}
    assert by_key["database"]["healthy"] is None
    assert by_key["database"]["detail"] == "not configured"
    assert by_key["event_bus"]["healthy"] is None
    assert by_key["event_bus"]["detail"] == "not configured"
