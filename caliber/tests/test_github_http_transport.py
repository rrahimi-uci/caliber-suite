"""Tests for the real ``httpx``-backed GitHub REST transport (`P4-E`).

Deterministic and offline: every request is served by an in-process
``httpx.MockTransport`` handler rather than a real network call, so these
tests exercise the actual request/response marshalling
(``HTTPXGitHubTransport``) without ever reaching github.com.
"""

from __future__ import annotations

import httpx
import pytest

from caliber.github_http_transport import HTTPXGitHubTransport, api_base_url
from caliber.github_source_control import GitHubTransportError


def test_api_base_url_for_github_com() -> None:
    assert api_base_url("github.com") == "https://api.github.com"
    assert api_base_url("") == "https://api.github.com"


def test_api_base_url_for_enterprise_server_host() -> None:
    assert api_base_url("github.example.com") == "https://github.example.com/api/v3"


def test_request_marshals_method_path_headers_params_and_json_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    transport = HTTPXGitHubTransport(client=client)

    response = transport.request(
        "POST",
        "/repos/owner/repo/statuses/deadbeef",
        headers={"Authorization": "Bearer tok"},
        params={"per_page": "10"},
        json_body={"state": "success"},
    )

    assert response.status_code == 200
    assert response.payload == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["path"] == "/repos/owner/repo/statuses/deadbeef"
    assert captured["params"] == {"per_page": "10"}
    assert captured["authorization"] == "Bearer tok"
    assert captured["body"] == b'{"state":"success"}'


def test_request_returns_empty_payload_for_a_body_less_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    transport = HTTPXGitHubTransport(client=client)

    response = transport.request("DELETE", "/repos/owner/repo", headers={})

    assert response.status_code == 204
    assert response.payload == {}


def test_request_returns_empty_payload_when_body_is_not_valid_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    transport = HTTPXGitHubTransport(client=client)

    response = transport.request("GET", "/repos/owner/repo", headers={})

    assert response.status_code == 200
    assert response.payload == {}


def test_request_translates_a_transport_level_failure_to_a_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    transport = HTTPXGitHubTransport(client=client)

    with pytest.raises(GitHubTransportError, match="ConnectError"):
        transport.request("GET", "/repos/owner/repo", headers={})


def test_close_disposes_only_a_client_this_instance_built() -> None:
    external_client = httpx.Client(base_url="https://api.github.com")
    transport = HTTPXGitHubTransport(client=external_client)
    transport.close()
    assert not external_client.is_closed

    owned_transport = HTTPXGitHubTransport(host="github.com")
    owned_transport.close()
    external_client.close()
