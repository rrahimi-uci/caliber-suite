"""Tests for the current-user resolver.

Identity resolution order (see :mod:`caliber.auth`): a validated session wins, then
the trusted header *only* in ``trusted_header`` mode, then the dev fallback *only*
when explicitly enabled. The header tests below therefore declare
``trusted_header`` — under the shipped ``session`` default the header is ignored
entirely, which is asserted at the bottom of this file.
"""

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
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
    }
    # Header-driven tests need a config that says the header is trusted. Defaulting
    # it here keeps each test about the resolver rather than about the mode; the
    # session-default behaviour is asserted explicitly below.
    resolved = config if config is not None else _trusted_header_config()
    scope["app"] = SimpleNamespace(state=SimpleNamespace(config=resolved))
    return Request(scope)


def _trusted_header_config(**overrides: str) -> CaliberConfig:
    return CaliberConfig.load(environ={"CALIBER_AUTH_MODE": "trusted_header", **overrides})


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
    """The dev fallback must be opted into: with the shipped admin lists it turned
    an unauthenticated request into an admin, which is half of C1."""
    request = _make_request(
        config=_trusted_header_config(
            CALIBER_DEV_USER="@local-admin",
            CALIBER_AUTH_DEV_FALLBACK_ENABLED="true",
        )
    )
    assert current_user(request) == "@local-admin"


def test_the_dev_fallback_is_off_by_default() -> None:
    """Regression (C1): a request with *no* identity header was an admin in the
    shipped Compose stack, because dev_user defaulted to @local-admin and every
    privileged list contained it."""
    request = _make_request(config=_trusted_header_config(CALIBER_DEV_USER="@local-admin"))
    assert current_user(request) == ANONYMOUS


def test_current_user_keeps_explicit_empty_header_anonymous_even_with_dev_user() -> None:
    request = _make_request(
        {"X-CALIBER-User": ""},
        config=CaliberConfig.load(environ={"CALIBER_DEV_USER": "@local-admin"}),
    )
    assert current_user(request) == ANONYMOUS


def test_current_user_prefers_header_over_configured_dev_user() -> None:
    request = _make_request(
        {"X-CALIBER-User": "@sarah"},
        config=_trusted_header_config(
            CALIBER_DEV_USER="@local-admin",
            CALIBER_AUTH_DEV_FALLBACK_ENABLED="true",
        ),
    )
    assert current_user(request) == "@sarah"


def test_current_user_ignores_invalid_configured_dev_user() -> None:
    request = _make_request(
        config=_trusted_header_config(
            CALIBER_DEV_USER="anonymous", CALIBER_AUTH_DEV_FALLBACK_ENABLED="true"
        )
    )
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


def test_literal_admin_identity_is_not_privileged_without_explicit_configuration() -> None:
    """The local bootstrap name must not become a global RBAC default.

    Trusted-header deployments may already have an unrelated identity named ``admin``;
    upgrading must not silently grant it every CALIBER scope when the local bootstrap is
    disabled. Loopback launchers set ``CALIBER_ADMIN_USERS=admin`` explicitly.
    """
    request = _make_request(
        {"X-CALIBER-User": "admin"},
        config=_trusted_header_config(),
    )

    identity = resolve_identity(request)

    assert identity.user_id == "admin"
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


# ── The shipped default: session mode ignores the header (C1) ───────────────


def test_session_mode_ignores_the_identity_header() -> None:
    """The core of C1: any client could assert any identity. Under the shipped
    default the header is not consulted at all, so it cannot."""
    request = _make_request(
        {"X-CALIBER-User": "@attacker"},
        config=CaliberConfig.load(environ={"CALIBER_AUTH_MODE": "session"}),
    )
    assert current_user(request) == ANONYMOUS
    with pytest.raises(HTTPException) as exc_info:
        require_user(request)
    assert exc_info.value.status_code == 401


def test_session_mode_ignores_the_dev_fallback_header_absence_too() -> None:
    request = _make_request(
        config=CaliberConfig.load(
            environ={
                "CALIBER_AUTH_MODE": "session",
                "CALIBER_DEV_USER": "@local-admin",
                "CALIBER_AUTH_DEV_FALLBACK_ENABLED": "true",
            }
        )
    )
    # The dev fallback is a *header-mode* convenience; it must not manufacture an
    # identity when passwords are the configured mechanism.
    assert current_user(request) == ANONYMOUS


def test_a_request_with_no_config_resolves_anonymous() -> None:
    """A half-built app must fail closed rather than trusting a header it cannot
    validate against any policy."""
    raw_headers = [(b"x-caliber-user", b"@someone")]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": raw_headers})
    assert current_user(request) == ANONYMOUS
