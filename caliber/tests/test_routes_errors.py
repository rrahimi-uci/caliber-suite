"""Tests for :mod:`caliber.routes._errors` — HTTP + validation error handlers."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request

from caliber.routes._errors import http_exception_handler, validation_error_handler


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
