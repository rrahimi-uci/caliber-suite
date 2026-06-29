"""Tests for the RBAC scope resolver + ``require_scopes`` helper.

The contract has three layers:

1. ``current_scopes(request)`` returns a ``frozenset[str]`` reflecting
   the active user's scopes, with admin > approver > operator > viewer
   inheritance applied.
2. ``require_scopes(request, scopes)`` raises 401 for anonymous, 403
   for signed-in-but-missing-scope, returns the user ID otherwise.
3. The user-list config fields (``admin_users`` etc.) accept the same
   comma-separated format whether they were set via env var or
   constructed directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.exceptions import HTTPException

from caliber.auth import (
    ANONYMOUS,
    SCOPE_ADMIN,
    SCOPE_APPROVER,
    SCOPE_OPERATOR,
    SCOPE_VIEWER,
    _parse_user_list,
    current_scopes,
    current_user,
    require_scopes,
)
from caliber.config import CaliberConfig

# ---------------------------------------------------------------------------
# Unit tests against a hand-built request stub
# ---------------------------------------------------------------------------


def _stub_request(user: str | None, config: CaliberConfig) -> MagicMock:
    """Build a minimal ``Request``-like object the resolver can read.

    The resolver only ever touches ``request.headers`` and
    ``request.app.state.config`` — a ``MagicMock`` is enough to drive
    the unit branches without spinning up Starlette.
    """
    request = MagicMock()
    request.headers = {} if user is None else {"X-CALIBER-User": user}
    request.app.state.config = config
    return request


def _config(**overrides: str) -> CaliberConfig:
    """Build a config with empty user-list defaults plus overrides."""
    base: dict[str, object] = {
        "admin_users": "",
        "approver_users": "",
        "operator_users": "",
    }
    base.update(overrides)
    return CaliberConfig(**base)


def test_parse_user_list_handles_whitespace_and_empties() -> None:
    assert _parse_user_list("") == frozenset()
    assert _parse_user_list("  ") == frozenset()
    assert _parse_user_list("@a, @b ,, @c") == frozenset({"@a", "@b", "@c"})
    # Trailing comma is fine — operator convenience.
    assert _parse_user_list("@a,") == frozenset({"@a"})


def test_current_scopes_anonymous_returns_empty() -> None:
    request = _stub_request(None, _config())
    assert current_scopes(request) == frozenset()
    # And current_user returns the sentinel string for audit.
    assert current_user(request) == ANONYMOUS


def test_current_scopes_authenticated_always_includes_viewer() -> None:
    """Any signed-in user gets viewer access — reads are open."""
    request = _stub_request("@anyone", _config())
    assert current_scopes(request) == frozenset({SCOPE_VIEWER})


def test_current_scopes_operator_user_gets_operator_plus_viewer() -> None:
    request = _stub_request("@op", _config(operator_users="@op"))
    assert current_scopes(request) == frozenset({SCOPE_OPERATOR, SCOPE_VIEWER})


def test_current_scopes_approver_user_gets_approver_plus_viewer() -> None:
    request = _stub_request("@review", _config(approver_users="@review"))
    assert current_scopes(request) == frozenset({SCOPE_APPROVER, SCOPE_VIEWER})


def test_current_scopes_admin_user_gets_everything() -> None:
    """Admin inherits every lower scope — that's the convention every
    role-based system uses, and the parity checklist §11 requires."""
    request = _stub_request("@admin", _config(admin_users="@admin"))
    assert current_scopes(request) == frozenset(
        {SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER}
    )


def test_current_scopes_multi_assignment_unions_correctly() -> None:
    """A user can appear in multiple lists; the resolver unions."""
    request = _stub_request(
        "@dual",
        _config(approver_users="@dual", operator_users="@dual"),
    )
    assert current_scopes(request) == frozenset({SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER})


def test_require_scopes_returns_user_when_authorized() -> None:
    request = _stub_request("@admin", _config(admin_users="@admin"))
    assert require_scopes(request, [SCOPE_ADMIN]) == "@admin"


def test_require_scopes_raises_401_for_anonymous() -> None:
    request = _stub_request(None, _config())
    with pytest.raises(HTTPException) as excinfo:
        require_scopes(request, [SCOPE_ADMIN])
    assert excinfo.value.status_code == 401


def test_require_scopes_raises_403_for_missing_scope() -> None:
    request = _stub_request("@viewer", _config())
    with pytest.raises(HTTPException) as excinfo:
        require_scopes(request, [SCOPE_ADMIN])
    assert excinfo.value.status_code == 403
    assert "missing required scope" in excinfo.value.detail.lower()


def test_require_scopes_accepts_any_match() -> None:
    """An approver should be admitted to an ``approver | admin``
    endpoint without needing the admin scope."""
    request = _stub_request("@review", _config(approver_users="@review"))
    assert require_scopes(request, [SCOPE_APPROVER, SCOPE_ADMIN]) == "@review"


def test_require_scopes_empty_set_is_a_developer_error() -> None:
    """Calling ``require_scopes`` with no scopes shouldn't silently
    succeed — that's the kind of bug that turns into a CVE."""
    request = _stub_request("@admin", _config(admin_users="@admin"))
    with pytest.raises(RuntimeError, match="empty scope set"):
        require_scopes(request, [])
