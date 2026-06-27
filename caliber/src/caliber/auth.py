"""Current-user resolution + RBAC scopes.

CALIBER inherits authentication from the MLflow server it's mounted on
(or an upstream reverse proxy), so it never implements its own login
flow. This module abstracts two things on top of that identity:

1. ``current_user(request) -> str`` — who the requester is. Reads the
   ``X-CALIBER-User`` header, falls back to :data:`ANONYMOUS`.
2. ``current_scopes(request) -> frozenset[str]`` — what they're
   allowed to do. Resolves the user against the configured admin /
   approver / operator user lists and returns the union of granted
   scopes (admins implicitly hold every lower scope).

Endpoints that mutate state use :func:`require_scopes` to assert a
caller holds at least one scope from a required set. The resolver
returning a ``frozenset`` rather than a list is intentional — it makes
"this user has these scopes" comparable as a value, easy to test, and
trivially serializable into an audit row.

For Phase 5 the assignment surface is config-driven: a comma-separated
list of user IDs per scope (see :class:`caliber.config.CaliberConfig`).
A future milestone can swap the resolver for a DB-backed assignment
table without changing any call site — the public signature stays
``(Request) -> frozenset[str]``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from starlette.exceptions import HTTPException
from starlette.requests import Request

from caliber.config import CaliberConfig

ANONYMOUS = "anonymous"
_USER_HEADER = "X-CALIBER-User"
# Active project for multi-user scoping, sent by the SPA on every request.
_PROJECT_HEADER = "X-CALIBER-Project"

# Scope vocabulary. Names match the implementation-parity checklist §11. Kept as module constants so a
# typo in a route handler fails at import time rather than at request
# time.
SCOPE_VIEWER: Final[str] = "caliber.viewer"
SCOPE_OPERATOR: Final[str] = "caliber.operator"
SCOPE_APPROVER: Final[str] = "caliber.approver"
SCOPE_ADMIN: Final[str] = "caliber.admin"

# Inheritance: holding a higher-tier scope implicitly grants the lower
# ones. Reads (viewer) are open to every authenticated user; admin
# unlocks every mutation. Approver sits between operator and admin
# because the approval-flow scope is more sensitive than feedback
# triage but doesn't include agent CRUD.
_SCOPE_IMPLIES: Final[dict[str, frozenset[str]]] = {
    SCOPE_ADMIN: frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER}),
    SCOPE_APPROVER: frozenset({SCOPE_APPROVER, SCOPE_VIEWER}),
    SCOPE_OPERATOR: frozenset({SCOPE_OPERATOR, SCOPE_VIEWER}),
    SCOPE_VIEWER: frozenset({SCOPE_VIEWER}),
}


def current_user(request: Request) -> str:
    """Return the identifier of the current requester.

    Falls back to :data:`ANONYMOUS` when no identity header is present
    and only consults the configured local-dev fallback when the header
    is truly absent. An explicitly empty or invalid ``X-CALIBER-User``
    header remains anonymous so callers cannot bypass auth checks by
    sending a blank value while a dev fallback user is configured.
    Production deployments should use :func:`require_user` to reject
    anonymous calls on mutating endpoints.

    A header value equal to the reserved :data:`ANONYMOUS` sentinel
    (case-insensitive) is rejected as anonymous — otherwise a client
    could pose as the sentinel and bypass auth checks that key off the
    literal. Values containing ``,`` are also rejected because the
    scope resolver parses user lists as comma-separated and would
    otherwise admit list-bypass payloads like ``"@alice,@admin"``.
    """
    raw_header = request.headers.get(_USER_HEADER)
    actor = _identity_or_anonymous(raw_header)
    if actor != ANONYMOUS:
        return actor
    if raw_header is None:
        return _configured_dev_user(request)
    return ANONYMOUS


def require_user(request: Request) -> str:
    """Like :func:`current_user` but raises 401 when no identity is present."""
    actor = current_user(request)
    if actor == ANONYMOUS:
        raise HTTPException(
            status_code=401,
            detail="authentication required (no identity in request)",
        )
    return actor


def scopes_for_user(config: Any, user: str) -> frozenset[str]:
    """Resolve a user's scopes from a config, without a request.

    The Request-free core of :func:`current_scopes`, so non-route callers (e.g.
    the Aria plan executor enforcing separation of duties) can check authority.
    Anonymous / empty users get an empty set; any other authenticated user
    carries at least :data:`SCOPE_VIEWER`.
    """
    if not user or user == ANONYMOUS:
        return frozenset()
    granted: set[str] = set()
    if user in _parse_user_list(config.admin_users):
        granted.update(_SCOPE_IMPLIES[SCOPE_ADMIN])
    if user in _parse_user_list(config.approver_users):
        granted.update(_SCOPE_IMPLIES[SCOPE_APPROVER])
    if user in _parse_user_list(config.operator_users):
        granted.update(_SCOPE_IMPLIES[SCOPE_OPERATOR])
    granted.add(SCOPE_VIEWER)
    return frozenset(granted)


def current_scopes(request: Request) -> frozenset[str]:
    """Resolve the active user's scopes.

    Reads the configured user lists off ``app.state.config`` (a
    :class:`CaliberConfig` instance). Anonymous users get an empty set
    so :func:`require_scopes` produces a clean 401 rather than a 403.
    Authenticated users always carry at least :data:`SCOPE_VIEWER` —
    matching the "reads are open to every signed-in user" convention.
    """
    user = current_user(request)
    if user == ANONYMOUS:
        return frozenset()
    return scopes_for_user(_get_config(request), user)


@dataclass(frozen=True)
class CaliberIdentity:
    """Resolved request identity for multi-user project scoping.

    Bundles who the caller is (``user_id``), what they can do (``scopes``),
    and which project is active (``active_project_id``, from the
    ``X-CALIBER-Project`` header) into one value an endpoint can pass to the
    scoping helper. ``user_id`` matches the ``owner`` column stored on
    resources.
    """

    user_id: str
    scopes: frozenset[str]
    active_project_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def resolve_identity(request: Request) -> CaliberIdentity:
    """Resolve user, scopes, and active project from the request.

    Reuses :func:`current_user` and :func:`current_scopes` so identity
    resolution stays in one place, and reads the active project from the
    ``X-CALIBER-Project`` header (``None`` when absent or blank).
    """
    raw_project = request.headers.get(_PROJECT_HEADER)
    project = raw_project.strip() if raw_project else ""
    return CaliberIdentity(
        user_id=current_user(request),
        scopes=current_scopes(request),
        active_project_id=project or None,
    )


def require_scopes(request: Request, scopes: Iterable[str]) -> str:
    """Assert the caller holds at least one of the named scopes.

    Returns the authenticated user ID so the caller can pass it straight
    to ``audit_record(actor=…)`` without re-resolving. Two failure
    modes:

    * No identity in the request → 401 (authentication required).
    * Identity present but missing every required scope → 403.

    The two-status split matches the parity checklist §11 expectation
    for an unauthenticated vs. unauthorized request.
    """
    required = frozenset(scopes)
    if not required:
        # Defensive: a misconfigured call site shouldn't accidentally
        # behave as "no auth required."
        raise RuntimeError("require_scopes called with empty scope set")

    actor = current_user(request)
    if actor == ANONYMOUS:
        raise HTTPException(
            status_code=401,
            detail="authentication required (no identity in request)",
        )
    granted = current_scopes(request)
    if required.isdisjoint(granted):
        raise HTTPException(
            status_code=403,
            detail=(
                f"missing required scope; expected one of {sorted(required)}, "
                f"have {sorted(granted)}"
            ),
        )
    return actor


def _parse_user_list(raw: str) -> frozenset[str]:
    """Split a comma-separated user list, ignoring empty entries.

    ``"@sarah, @alex,"`` → ``{"@sarah", "@alex"}``. Trims whitespace so
    operators don't have to be precious about how the env var is
    formatted.
    """
    return frozenset(entry.strip() for entry in raw.split(",") if entry.strip())


def _identity_or_anonymous(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value or value.lower() == ANONYMOUS or "," in value:
        return ANONYMOUS
    return value


def _configured_dev_user(request: Request) -> str:
    """Return configured local-dev identity, or anonymous when unset/invalid."""
    try:
        config = _get_config(request)
    except (AttributeError, KeyError, RuntimeError):
        return ANONYMOUS
    return _identity_or_anonymous(config.dev_user)


def _get_config(request: Request) -> CaliberConfig:
    config: CaliberConfig = request.app.state.config
    return config
