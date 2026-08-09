"""Transport behaviour: envelopes, errors, retries, CSRF, headers, paging."""

from __future__ import annotations

import httpx
import pytest

from caliber_sdk import (
    API_PREFIX,
    CaliberAPIError,
    CaliberAuthenticationError,
    CaliberConfigError,
    CaliberNotFoundError,
    CaliberServerError,
    CaliberTransportError,
    CaliberValidationError,
    TokenAuth,
    Transport,
)

BASE = "https://caliber.test"


def transport_with(handler: object, **kwargs: object) -> Transport:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return Transport(BASE, client=client, **kwargs)  # type: ignore[arg-type]


def test_envelope_is_unwrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{API_PREFIX}/prompts"
        return httpx.Response(200, json={"data": [{"name": "intake"}]})

    with transport_with(handler) as transport:
        assert transport.get("/prompts").data == [{"name": "intake"}]


def test_an_unenveloped_body_is_returned_as_is() -> None:
    """The OpenAPI document is served unenveloped; unwrapping would yield None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openapi": "3.0.3", "paths": {}})

    with transport_with(handler) as transport:
        assert transport.get("/openapi.json").data["openapi"] == "3.0.3"


def test_a_body_with_data_plus_other_keys_is_not_unwrapped() -> None:
    """Only the exact ``{"data": ...}`` envelope is stripped.

    A payload that merely *contains* a ``data`` field is a real payload, and
    unwrapping it would silently discard its siblings.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [1], "total": 1})

    with transport_with(handler) as transport:
        assert transport.get("/things").data == {"data": [1], "total": 1}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, CaliberAuthenticationError),
        (404, CaliberNotFoundError),
        (500, CaliberServerError),
    ],
)
def test_error_statuses_become_typed_exceptions(
    status: int, expected: type[CaliberAPIError]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope", "status_code": status})

    with transport_with(handler, max_retries=0) as transport, pytest.raises(expected) as caught:
        transport.get("/prompts")
    assert caught.value.status_code == status
    assert caught.value.detail == "nope"
    assert "GET" in str(caught.value)


def test_structured_validation_errors_name_their_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "detail": "request body validation failed",
                "status_code": 400,
                "errors": [{"loc": ["body", "name"], "msg": "field required", "type": "missing"}],
            },
        )

    with transport_with(handler) as transport, pytest.raises(CaliberValidationError) as caught:
        transport.post("/prompts", json={})
    assert caught.value.errors[0]["msg"] == "field required"
    assert "body.name" in str(caught.value)


def test_a_non_json_error_body_still_produces_a_usable_exception() -> None:
    """A proxy's HTML error page must not become a decode traceback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    with (
        transport_with(handler, max_retries=0) as transport,
        pytest.raises(CaliberServerError) as caught,
    ):
        transport.get("/prompts")
    assert "Bad Gateway" in (caught.value.detail or "")


def test_idempotent_requests_retry_on_transient_status() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"detail": "warming up"})
        return httpx.Response(200, json={"data": "ok"})

    with transport_with(handler, max_retries=2, backoff_factor=0.0) as transport:
        assert transport.get("/health").data == "ok"
    assert calls["n"] == 2


def test_writes_are_never_retried() -> None:
    """A POST that reached the server may already have had its effect.

    The SDK cannot tell whether a 503 happened before or after the write, so
    retrying could duplicate it. Failing is the safe answer.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "warming up"})

    with (
        transport_with(handler, max_retries=3, backoff_factor=0.0) as transport,
        pytest.raises(CaliberServerError),
    ):
        transport.post("/prompts", json={"name": "x"})
    assert calls["n"] == 1


def test_connection_failures_raise_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with transport_with(handler, max_retries=0) as transport, pytest.raises(CaliberTransportError):
        transport.get("/health")


def test_auth_project_and_correlation_headers_are_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {}})

    with transport_with(handler, auth=TokenAuth("calpat_abc"), project="PRJ-1") as transport:
        transport.get("/me")

    assert seen["authorization"] == "Bearer calpat_abc"
    assert seen["x-caliber-project"] == "PRJ-1"
    assert seen["x-request-id"]
    assert "caliber-sdk-python" in seen["user-agent"]


def test_a_write_refused_for_csrf_bootstraps_and_replays_once() -> None:
    """Recoverable and invisible, but bounded: exactly one replay."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/csrf"):
            return httpx.Response(200, json={"data": {"csrf_token": "tok-1"}})
        if request.method == "POST" and "x-caliber-csrf" not in request.headers:
            return httpx.Response(403, json={"detail": "CSRF token missing", "status_code": 403})
        return httpx.Response(201, json={"data": {"ok": True}})

    with transport_with(handler) as transport:
        assert transport.post("/prompts", json={"name": "x"}).data == {"ok": True}

    assert calls == [
        f"POST {API_PREFIX}/prompts",
        f"GET {API_PREFIX}/csrf",
        f"POST {API_PREFIX}/prompts",
    ]


def test_a_genuine_permission_failure_is_not_mistaken_for_csrf() -> None:
    """A 403 without a CSRF hint must surface, not trigger a bootstrap loop."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"detail": "requires caliber.operator", "status_code": 403})

    with transport_with(handler) as transport, pytest.raises(Exception) as caught:
        transport.post("/prompts", json={})
    assert "operator" in str(caught.value)
    assert calls["n"] == 1


def test_paths_may_be_given_with_or_without_the_prefix() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": None})

    with transport_with(handler) as transport:
        transport.get("/prompts")
        transport.get(f"{API_PREFIX}/prompts")
    assert seen == [f"{API_PREFIX}/prompts"] * 2


def test_paginate_walks_offsets_and_stops_on_a_short_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        page = [{"i": offset}, {"i": offset + 1}] if offset < 4 else [{"i": 4}]
        return httpx.Response(200, json={"data": page})

    with transport_with(handler) as transport:
        assert [item["i"] for item in transport.paginate("/prompts", limit=2)] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("bad", ["", "   ", "caliber.test", "ftp://caliber.test"])
def test_base_url_must_be_an_http_url(bad: str) -> None:
    with pytest.raises(CaliberConfigError):
        Transport(bad)
