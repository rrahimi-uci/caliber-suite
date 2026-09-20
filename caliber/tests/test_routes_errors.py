"""Tests for :mod:`caliber.routes._errors` — HTTP + validation error handlers."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request

from caliber.routes._errors import (
    CaliberHTTPException,
    http_exception_handler,
    validation_error_handler,
)


class _Dummy(BaseModel):
    n: int


@pytest.mark.asyncio
async def test_http_exception_handler_renders_json() -> None:
    exc = HTTPException(status_code=404, detail="not found")
    # Build a minimal ASGI scope; the handler ignores the request.
    scope = {"type": "http", "method": "GET", "path": "/test", "headers": []}
    request = Request(scope)
    resp = await http_exception_handler(request, exc)
    assert resp.status_code == 404
    body = resp.body
    import json

    data = json.loads(body)
    assert data["detail"] == "not found"
    assert data["status_code"] == 404


@pytest.mark.asyncio
async def test_http_exception_handler_omits_reason_code_for_a_bare_exception() -> None:
    """A bare ``HTTPException`` (still the overwhelming majority of call
    sites) must render exactly as it did before ``reason_code`` existed --
    no extra key at all -- so this stays additive rather than breaking."""
    exc = HTTPException(status_code=404, detail="not found")
    scope = {"type": "http", "method": "GET", "path": "/test", "headers": []}
    request = Request(scope)
    resp = await http_exception_handler(request, exc)
    data = json.loads(resp.body)
    assert "reason_code" not in data


@pytest.mark.asyncio
async def test_http_exception_handler_renders_reason_code_when_set() -> None:
    """Phase 0 item 6: a migrated route raises :class:`CaliberHTTPException`
    with an explicit ``reason_code``, and the envelope must carry it
    alongside the unchanged human-readable ``detail``."""
    exc = CaliberHTTPException(
        status_code=409,
        detail="resource_type_adapter_unavailable: no adapter for 'widget'",
        reason_code="resource_type_adapter_unavailable",
    )
    scope = {"type": "http", "method": "POST", "path": "/test", "headers": []}
    request = Request(scope)
    resp = await http_exception_handler(request, exc)
    assert resp.status_code == 409
    data = json.loads(resp.body)
    assert data["status_code"] == 409
    assert data["detail"] == "resource_type_adapter_unavailable: no adapter for 'widget'"
    assert data["reason_code"] == "resource_type_adapter_unavailable"


@pytest.mark.asyncio
async def test_caliber_http_exception_defaults_reason_code_to_none() -> None:
    """A :class:`CaliberHTTPException` raised without an explicit
    ``reason_code`` behaves exactly like a bare ``HTTPException`` -- the
    field is opt-in per raise site, not implied by the subclass alone."""
    exc = CaliberHTTPException(status_code=400, detail="bad request")
    scope = {"type": "http", "method": "GET", "path": "/test", "headers": []}
    request = Request(scope)
    resp = await http_exception_handler(request, exc)
    data = json.loads(resp.body)
    assert "reason_code" not in data


@pytest.mark.asyncio
async def test_caliber_http_exception_still_forwards_headers() -> None:
    """The ``reason_code`` addition must not regress the existing
    header-forwarding fix (e.g. ``Retry-After`` on a 429)."""
    exc = CaliberHTTPException(
        status_code=429,
        detail="too many requests",
        reason_code="rate_limited",
        headers={"Retry-After": "30"},
    )
    scope = {"type": "http", "method": "GET", "path": "/test", "headers": []}
    request = Request(scope)
    resp = await http_exception_handler(request, exc)
    assert resp.headers["retry-after"] == "30"
    data = json.loads(resp.body)
    assert data["reason_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_http_exception_handler_rejects_non_http_exception() -> None:
    """If somehow called with a non-HTTPException, it should re-raise."""
    scope = {"type": "http", "method": "GET", "path": "/test", "headers": []}
    request = Request(scope)
    with pytest.raises(ValueError, match="bad"):
        await http_exception_handler(request, ValueError("bad"))


@pytest.mark.asyncio
async def test_validation_error_handler_renders_structured_400() -> None:
    try:
        _Dummy.model_validate({"n": "not-an-int"})
    except ValidationError as exc:
        scope = {"type": "http", "method": "POST", "path": "/test", "headers": []}
        request = Request(scope)
        resp = await validation_error_handler(request, exc)
        import json

        data = json.loads(resp.body)
        assert resp.status_code == 400
        assert data["detail"] == "request body validation failed"
        assert data["status_code"] == 400
        assert len(data["errors"]) >= 1
        return
    pytest.fail("ValidationError was not raised")


@pytest.mark.asyncio
async def test_validation_error_handler_rejects_non_validation_error() -> None:
    scope = {"type": "http", "method": "POST", "path": "/test", "headers": []}
    request = Request(scope)
    with pytest.raises(TypeError, match="wrong"):
        await validation_error_handler(request, TypeError("wrong"))
