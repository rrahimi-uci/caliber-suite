"""Tests for the current-user resolver."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from caliber.auth import (
    ANONYMOUS,
    SCOPE_ADMIN,
    SCOPE_VIEWER,
    current_user,
    require_user,
    resolve_identity,
)
from caliber.config import CaliberConfig


def _make_request(
    headers: dict[str, str] | None = None,
    *,
    config: CaliberConfig | None = None,
) -> Request:
    """Build a minimal Starlette Request directly from an ASGI scope.

    Using the constructor avoids spinning up a full TestClient just to assert
    on header parsing.
    """
    headers = headers or {}
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
    }
    if config is not None:
        scope["app"] = SimpleNamespace(state=SimpleNamespace(config=config))
    return Request(scope)


def test_current_user_returns_anonymous_when_no_header() -> None:
    request = _make_request()
    assert current_user(request) == ANONYMOUS


def test_current_user_returns_header_value() -> None:
    request = _make_request({"X-CALIBER-User": "@sarah"})
    assert current_user(request) == "@sarah"


def test_require_user_raises_when_anonymous() -> None:
    request = _make_request()
    with pytest.raises(HTTPException) as exc_info:
        require_user(request)
    assert exc_info.value.status_code == 401


def test_require_user_returns_identity_when_present() -> None:
    request = _make_request({"X-CALIBER-User": "@reza"})
    assert require_user(request) == "@reza"


def test_current_user_uses_configured_dev_user_when_header_missing() -> None:
    request = _make_request(config=CaliberConfig.load(environ={"CALIBER_DEV_USER": "@local-admin"}))
    assert current_user(request) == "@local-admin"


def test_current_user_keeps_explicit_empty_header_anonymous_even_with_dev_user() -> None:
    request = _make_request(
        {"X-CALIBER-User": ""},
        config=CaliberConfig.load(environ={"CALIBER_DEV_USER": "@local-admin"}),
    )
    assert current_user(request) == ANONYMOUS


def test_current_user_prefers_header_over_configured_dev_user() -> None:
    request = _make_request(
        {"X-CALIBER-User": "@sarah"},
        config=CaliberConfig.load(environ={"CALIBER_DEV_USER": "@local-admin"}),
    )
    assert current_user(request) == "@sarah"


def test_current_user_ignores_invalid_configured_dev_user() -> None:
    request = _make_request(config=CaliberConfig.load(environ={"CALIBER_DEV_USER": "anonymous"}))
    assert current_user(request) == ANONYMOUS


# ── resolve_identity (multi-user project scoping) ──────────────────────────


def test_resolve_identity_bundles_user_scopes_and_project() -> None:
    request = _make_request(
        {"X-CALIBER-User": "@boss", "X-CALIBER-Project": "PRJ-123"},
        config=CaliberConfig.load(environ={"CALIBER_ADMIN_USERS": "@boss"}),
    )
    identity = resolve_identity(request)
    assert identity.user_id == "@boss"
    assert identity.active_project_id == "PRJ-123"
    assert identity.has_scope(SCOPE_ADMIN) is True
    assert identity.has_scope(SCOPE_VIEWER) is True  # admin implies viewer


def test_resolve_identity_no_project_header_is_none() -> None:
    request = _make_request(
        {"X-CALIBER-User": "@viewer"},
        config=CaliberConfig.load(environ={"CALIBER_ADMIN_USERS": "@boss"}),
    )
    identity = resolve_identity(request)
    assert identity.user_id == "@viewer"
    assert identity.active_project_id is None
    # A user not in any scope list is viewer-only, not admin.
    assert identity.has_scope(SCOPE_ADMIN) is False
    assert identity.has_scope(SCOPE_VIEWER) is True


def test_resolve_identity_blank_project_header_is_none() -> None:
    request = _make_request(
        {"X-CALIBER-User": "@viewer", "X-CALIBER-Project": "   "},
        config=CaliberConfig.load(environ={}),
    )
    assert resolve_identity(request).active_project_id is None


def test_resolve_identity_anonymous_has_no_scopes() -> None:
    request = _make_request(config=CaliberConfig.load(environ={}))
    identity = resolve_identity(request)
    assert identity.user_id == ANONYMOUS
    assert identity.scopes == frozenset()
    assert identity.has_scope(SCOPE_VIEWER) is False
