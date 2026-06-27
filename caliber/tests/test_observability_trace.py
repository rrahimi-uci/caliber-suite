"""Tests for the trace-context primitives + middleware."""

from __future__ import annotations

from starlette.testclient import TestClient

from caliber.observability.trace import (
    bind_trace_id,
    current_trace_id,
    new_trace_id,
)


def test_current_trace_id_defaults_to_empty_string() -> None:
    assert current_trace_id() == ""


def test_bind_trace_id_sets_and_resets() -> None:
    assert current_trace_id() == ""
    with bind_trace_id("abc123"):
        assert current_trace_id() == "abc123"
    # Reset after context exit.
    assert current_trace_id() == ""


def test_bind_trace_id_generates_a_fresh_id_when_none() -> None:
    with bind_trace_id() as trace_id:
        assert trace_id == current_trace_id()
        assert len(trace_id) == 16


def test_new_trace_id_is_hex_and_unique() -> None:
    a = new_trace_id()
    b = new_trace_id()
    assert a != b
    assert all(c in "0123456789abcdef" for c in a)


def test_middleware_generates_trace_id_when_inbound_missing(client: TestClient) -> None:
    response = client.get("/ajax-api/2.0/mlflow/caliber/health")
    assert response.status_code == 200
    # Outbound echo so log aggregators on the proxy side can correlate.
    assert "x-request-id" in {k.lower() for k in response.headers}
    echoed = response.headers["x-request-id"]
    assert len(echoed) == 16


def test_middleware_propagates_inbound_x_request_id(client: TestClient) -> None:
    response = client.get(
        "/ajax-api/2.0/mlflow/caliber/health",
        headers={"X-Request-Id": "client-supplied-123"},
    )
    assert response.headers["x-request-id"] == "client-supplied-123"


def test_middleware_propagates_inbound_x_trace_id(client: TestClient) -> None:
    """Older SDKs send ``X-Trace-Id`` instead of ``X-Request-Id``."""
    response = client.get(
        "/ajax-api/2.0/mlflow/caliber/health",
        headers={"X-Trace-Id": "sdk-supplied-456"},
    )
    assert response.headers["x-request-id"] == "sdk-supplied-456"


def test_middleware_caps_oversized_inbound_id(client: TestClient) -> None:
    """A pathological 10 KB header value gets truncated to 64 chars."""
    big = "a" * 10_000
    response = client.get("/ajax-api/2.0/mlflow/caliber/health", headers={"X-Request-Id": big})
    assert len(response.headers["x-request-id"]) == 64


def test_middleware_skips_non_http_scope(client: TestClient) -> None:
    """The middleware passes through non-HTTP scopes without setting a trace."""
    from caliber.observability.trace import TraceIdMiddleware

    async def inner(scope, receive, send):
        pass

    mw = TraceIdMiddleware(inner)
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mw({"type": "websocket", "headers": []}, None, None))
    finally:
        loop.close()


def test_read_inbound_header_skips_non_ascii_values() -> None:
    """Non-ASCII header bytes are silently skipped."""
    from caliber.observability.trace import _read_inbound_header

    # A scope with a header that has non-ASCII bytes.
    scope = {
        "headers": [
            (b"x-request-id", b"\xff\xfe\xfd"),
        ]
    }
    result = _read_inbound_header(scope)
    assert result is None


def test_read_inbound_header_skips_empty_values() -> None:
    """Empty header values after stripping are skipped."""
    from caliber.observability.trace import _read_inbound_header

    scope = {
        "headers": [
            (b"x-request-id", b"   "),
        ]
    }
    result = _read_inbound_header(scope)
    assert result is None


def test_read_inbound_header_no_headers() -> None:
    """A scope with no headers at all returns None."""
    from caliber.observability.trace import _read_inbound_header

    assert _read_inbound_header({}) is None
