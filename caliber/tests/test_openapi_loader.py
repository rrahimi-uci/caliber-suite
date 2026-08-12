"""Tests for OpenAPI spec loading: inline, upload, and URL fetch.

The URL-fetch tests use a real local HTTP server rather than mocking
``build_client`` — the same principle as ``test_openapi_executor_egress.py``: a
loader that silently drops its ``policy=`` argument must fail loudly here, not
pass behind a stub.
"""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from caliber.egress import EgressPolicy
from caliber.integrations.openapi.loader import (
    OpenApiLoadError,
    load_inline_spec,
    load_spec,
    load_spec_from_url,
    load_uploaded_spec,
    probe_spec_source,
)

SPEC_TEXT = """
openapi: 3.0.3
info: {title: Ticket API, version: "1"}
paths:
  /tickets:
    get: {responses: {"200": {description: ok}}}
"""

ALLOW_LOOPBACK = EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"}))


class _Handler(BaseHTTPRequestHandler):
    body = SPEC_TEXT
    status = 200
    content_type = "application/yaml"
    redirect_to: str | None = None

    def do_GET(self) -> None:
        if type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        payload = type(self).body.encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:
        self.send_response(type(self).status)
        self.send_header("Content-Type", type(self).content_type)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        """Keep test output clean."""


@pytest.fixture
def local_server():
    _Handler.body = SPEC_TEXT
    _Handler.status = 200
    _Handler.content_type = "application/yaml"
    _Handler.redirect_to = None
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _url(server: HTTPServer) -> str:
    return f"http://{server.server_address[0]}:{server.server_address[1]}/openapi.yaml"


# --- inline -----------------------------------------------------------------


def test_load_inline_spec_returns_the_text_verbatim() -> None:
    loaded = load_inline_spec(SPEC_TEXT, source_ref="pasted")
    assert loaded.spec_text == SPEC_TEXT
    assert loaded.source_kind == "inline_text"


def test_load_inline_spec_rejects_blank_text() -> None:
    with pytest.raises(OpenApiLoadError):
        load_inline_spec("   ")


def test_load_inline_spec_rejects_oversized_input() -> None:
    with pytest.raises(OpenApiLoadError):
        load_inline_spec("x" * (16 * 1024 * 1024 + 1))


# --- upload -------------------------------------------------------------


def test_load_uploaded_spec_decodes_base64() -> None:
    encoded = base64.b64encode(SPEC_TEXT.encode("utf-8")).decode("ascii")
    loaded = load_uploaded_spec(encoded, source_ref="ticket-api.yaml")
    assert loaded.spec_text == SPEC_TEXT
    assert loaded.source_kind == "upload"


def test_load_uploaded_spec_rejects_invalid_base64() -> None:
    with pytest.raises(OpenApiLoadError):
        load_uploaded_spec("not-valid-base64!!!")


def test_load_uploaded_spec_rejects_non_utf8_bytes() -> None:
    encoded = base64.b64encode(b"\xff\xfe\x00\x01").decode("ascii")
    with pytest.raises(OpenApiLoadError):
        load_uploaded_spec(encoded)


def test_load_uploaded_spec_rejects_empty_payload() -> None:
    with pytest.raises(OpenApiLoadError):
        load_uploaded_spec(base64.b64encode(b"   ").decode("ascii"))


# --- url: egress policy --------------------------------------------------


def test_url_fetch_is_blocked_by_default_policy() -> None:
    """The regression case: loopback must be refused with no policy passed."""

    with pytest.raises(OpenApiLoadError) as excinfo:
        load_spec_from_url("http://127.0.0.1:9/openapi.yaml")
    message = str(excinfo.value).lower()
    assert "typeerror" not in message
    assert "loopback" in message or "blocked" in message


def test_url_fetch_rejects_non_http_schemes() -> None:
    with pytest.raises(OpenApiLoadError):
        load_spec_from_url("file:///etc/passwd", egress_policy=ALLOW_LOOPBACK)


def test_link_local_metadata_url_is_blocked() -> None:
    with pytest.raises(OpenApiLoadError):
        load_spec_from_url("http://169.254.169.254/latest/meta-data/")


# --- url: real fetch over an allowed loopback server -----------------------


def test_url_fetch_succeeds_against_an_allowed_host(local_server) -> None:
    loaded = load_spec_from_url(_url(local_server), egress_policy=ALLOW_LOOPBACK)
    assert loaded.spec_text.strip() == SPEC_TEXT.strip()
    assert loaded.source_kind == "url"
    assert loaded.source_ref == _url(local_server)


def test_url_fetch_reports_upstream_4xx(local_server) -> None:
    _Handler.status = 404
    with pytest.raises(OpenApiLoadError, match="404"):
        load_spec_from_url(_url(local_server), egress_policy=ALLOW_LOOPBACK)


def test_url_fetch_does_not_follow_redirects(local_server) -> None:
    _Handler.redirect_to = "http://169.254.169.254/steal-me"
    with pytest.raises(OpenApiLoadError, match="redirect"):
        load_spec_from_url(_url(local_server), egress_policy=ALLOW_LOOPBACK)


def test_url_fetch_rejects_an_oversized_response(local_server) -> None:
    _Handler.body = "x" * (16 * 1024 * 1024 + 1)
    with pytest.raises(OpenApiLoadError, match="byte import limit"):
        load_spec_from_url(_url(local_server), egress_policy=ALLOW_LOOPBACK)


def test_load_spec_dispatches_on_source_kind(local_server) -> None:
    loaded = load_spec(
        source_kind="url",
        spec_url=_url(local_server),
        egress_policy=ALLOW_LOOPBACK,
    )
    assert loaded.source_kind == "url"


def test_load_spec_rejects_unknown_source_kind() -> None:
    with pytest.raises(OpenApiLoadError):
        load_spec(source_kind="ftp")


# --- probe_spec_source ------------------------------------------------------


def test_probe_reports_reachable_for_an_allowed_url(local_server) -> None:
    result = probe_spec_source(
        source_kind="url", spec_url=_url(local_server), egress_policy=ALLOW_LOOPBACK
    )
    assert result["reachable"] is True
    assert result["allowed"] is True


def test_probe_reports_blocked_for_a_disallowed_host() -> None:
    result = probe_spec_source(source_kind="url", spec_url="http://127.0.0.1:9/spec.yaml")
    assert result["allowed"] is False
    assert result["reachable"] is False


def test_probe_treats_non_url_sources_as_trivially_reachable() -> None:
    result = probe_spec_source(source_kind="inline_text")
    assert result["reachable"] is True
    assert result["allowed"] is True
