"""Current-user resolution + RBAC scopes.

This module owns **resolution order** — how a request's identity is established
and in what precedence. :mod:`caliber.sessions` owns credential and session rules;
:mod:`caliber.routes.auth` owns the HTTP endpoints.

Resolution order (first match wins), closing C1:

1. **A validated session** (``caliber_session`` cookie or ``Authorization: Bearer``).
   The token is looked up in ``caliber_sessions``, so a revoked session, an expired
   session, and a session whose account has since been disabled are all rejected.
   Nothing here is client-asserted.
2. **A trusted header**, only when ``auth_mode == "trusted_header"``. When
   ``auth_trusted_proxy_secret_env`` is configured, the request must also carry the
   matching ``X-CALIBER-Proxy-Secret``, so bypassing the proxy is not enough to
   assert an identity.
3. **The dev fallback**, only in ``trusted_header`` mode, only when
   ``auth_dev_fallback_enabled`` is true, and only when the request carries no
   identity header at all. Off by default — with the shipped admin lists it
   previously turned an unauthenticated request into an admin.
4. Otherwise :data:`ANONYMOUS`.

The header path is now a deliberate, documented deployment mode rather than the
unconditional behaviour. In the default ``session`` mode ``X-CALIBER-User`` is
**ignored entirely**, which is what makes the authorization predicates elsewhere in
this codebase enforce a real boundary.

Two functions remain the public surface:

1. ``current_user(request) -> str`` — who the requester is.
2. ``current_scopes(request) -> frozenset[str]`` — what they're
   allowed to do. Resolves the user against the configured admin /
   approver / operator user lists and returns the union of granted
   scopes (admins implicitly hold every lower scope).

Endpoints that mutate state use :func:`require_scopes` to assert a
caller holds at least one scope from a required set. The resolver
returning a ``frozenset`` rather than a list is intentional — it makes
"this user has these scopes" comparable as a value, easy to test, and
trivially serializable into an audit row.

Scope *assignment* remains config-driven: a comma-separated list of user IDs per
scope (see :class:`caliber.config.CaliberConfig`). That is deliberate and separate
from C1 — authentication answers "is this really them?", which is the question that
was unanswered; moving authorization into a database table is its own change and can
happen without touching any call site, because the public signature stays
``(Request) -> frozenset[str]``.
"""

from __future__ import annotations

import hmac
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
# Shared secret a trusted proxy presents so a request that bypasses it is rejected.
_PROXY_SECRET_HEADER = "X-CALIBER-Proxy-Secret"  # noqa: S105 - a header name, not a secret

AUTH_MODE_SESSION = "session"
AUTH_MODE_TRUSTED_HEADER = "trusted_header"

# Resolved identity is cached on the request scope: several handlers call
# current_user() and resolve_identity() in one request, and each session lookup is a
# database round trip. Keyed per-request, so it cannot outlive the request or leak
# across them.
_SCOPE_CACHE_KEY = "caliber_resolved_user"
#: Scopes a personal access token asked for, recorded during identity
#: resolution. Absent for every other credential, which is what makes the
#: intersection in :func:`current_scopes` a no-op for sessions and headers.
_PAT_SCOPES_KEY = "caliber_pat_requested_scopes"

# Scope vocabulary. Names match the implementation-parity checklist §11. Kept as module constants so a
# typo in a route handler fails at import time rather than at request
# time.
SCOPE_VIEWER: Final[str] = "caliber.viewer"
SCOPE_OPERATOR: Final[str] = "caliber.operator"
SCOPE_APPROVER: Final[str] = "caliber.approver"
SCOPE_ADMIN: Final[str] = "caliber.admin"

#: Every scope the system defines. Used to reject a token requesting a scope
#: name that does not exist -- otherwise a typo becomes a silently powerless
#: token whose failures surface far from their cause.
ALL_SCOPES: Final[frozenset[str]] = frozenset(
    {SCOPE_VIEWER, SCOPE_OPERATOR, SCOPE_APPROVER, SCOPE_ADMIN}
)

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

    See the module docstring for resolution order. Returns :data:`ANONYMOUS` when no
    identity can be established; :func:`require_user` turns that into a 401.

    A header value equal to the reserved :data:`ANONYMOUS` sentinel
    (case-insensitive) is rejected — otherwise a client could pose as the sentinel
    and bypass checks that key off the literal. Values containing ``,`` are also
    rejected because the scope resolver parses user lists as comma-separated and
    would otherwise admit list-bypass payloads like ``"@alice,@admin"``.
    """
    cached = request.scope.get(_SCOPE_CACHE_KEY)
    if isinstance(cached, str):
        return cached
    resolved = _resolve_user(request)
    request.scope[_SCOPE_CACHE_KEY] = resolved
    return resolved


def _resolve_user(request: Request) -> str:
    config = _config_or_none(request)
    mode = str(getattr(config, "auth_mode", AUTH_MODE_SESSION) or AUTH_MODE_SESSION)

    # 1. A validated server-side session always wins, in every mode: a real
    #    credential must not be overridable by a header.
    session_user = _session_user(request, config)
    if session_user is not None:
        return session_user

    raw_header = request.headers.get(_USER_HEADER)

    # 2/3. Header trust and the dev fallback are both *header-mode* mechanisms. In
    # session mode neither applies: passwords are the configured mechanism, and
    # manufacturing an identity from a header or from config would contradict that.
    if mode != AUTH_MODE_TRUSTED_HEADER:
        return ANONYMOUS
    if not _proxy_secret_ok(request, config):
        return ANONYMOUS
    actor = _identity_or_anonymous(raw_header)
    if actor != ANONYMOUS:
        return actor
    # The fallback applies only when no header was sent at all, so an explicitly
    # blank header cannot be used to pick up an ambient privileged identity.
    if raw_header is None and bool(getattr(config, "auth_dev_fallback_enabled", False)):
        return _identity_or_anonymous(getattr(config, "dev_user", ""))
    return ANONYMOUS


def session_token_from_request(request: Request) -> str | None:
    """Extract a session token from the cookie or an ``Authorization: Bearer`` header.

    The header form exists for API clients and CLI use; the cookie form is what the
    SPA uses, and being HttpOnly is why the SPA cannot leak it to injected script.
    """
    config = _config_or_none(request)
    cookie_name = str(getattr(config, "auth_session_cookie_name", "caliber_session"))
    # ``isinstance`` rather than truthiness: a non-string cookie value is not a
    # token, and treating one as such would send junk to the session lookup.
    cookie = request.cookies.get(cookie_name)
    if isinstance(cookie, str) and cookie:
        return cookie
    header = request.headers.get("Authorization", "")
    if isinstance(header, str) and header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _session_user(request: Request, config: Any) -> str | None:
    """Resolve a bearer credential against the database, or ``None``.

    Handles both credential kinds that arrive the same way. A personal access
    token is distinguished by its ``calpat_`` prefix, so the branch costs no
    extra query: a session token can never match it, and vice versa.

    When a PAT authenticates the request, the scopes it *requested* are stashed
    on the request scope for :func:`current_scopes` to intersect with what the
    owner actually holds. The token is a ceiling, never a grant.
    """
    del config
    token = session_token_from_request(request)
    if not token:
        return None
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return None
    from caliber.sessions import (  # noqa: PLC0415
        PAT_PREFIX,
        resolve_personal_access_token,
        resolve_session,
    )

    try:
        with factory() as db:
            if token.startswith(PAT_PREFIX):
                resolved = resolve_personal_access_token(db, token)
                if resolved is None:
                    return None
                user_id, requested_scopes = resolved
                request.scope[_PAT_SCOPES_KEY] = requested_scopes
                return user_id
            return resolve_session(db, token)
    except Exception:  # a broken session store must not 500 every route
        return None


def _proxy_secret_ok(request: Request, config: Any) -> bool:
    """Whether the trusted-proxy shared secret requirement is satisfied.

    Unset means "not required", which preserves the behaviour of a deployment that
    genuinely terminates all traffic at a proxy. Set means a request without the
    matching header is rejected even in trusted_header mode, so an attacker who can
    reach the app port directly cannot assert an identity.
    """
    source = str(getattr(config, "auth_trusted_proxy_secret_env", "") or "").strip()
    if not source:
        return True
    from caliber.secrets import resolve_secret  # noqa: PLC0415

    expected = resolve_secret(source) or ""
    if not expected:
        # Configured but unresolvable: fail closed. A missing secret must not
        # silently downgrade to "no secret required".
        return False
    presented = request.headers.get(_PROXY_SECRET_HEADER, "")
    return hmac.compare_digest(presented, expected)


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
    granted = scopes_for_user(_get_config(request), user)

    # A personal access token narrows, never widens. Intersecting rather than
    # substituting is the whole safety property: a token issued while its owner
    # was an admin must stop conferring admin the moment they are demoted, and a
    # token cannot request authority its owner never had.
    #
    # An empty scope set means "inherit the owner's scopes" -- the documented
    # default for a token issued without an explicit ceiling -- so it must not be
    # treated as "no authority", which an unconditional intersection would do.
    requested = request.scope.get(_PAT_SCOPES_KEY)
    if isinstance(requested, frozenset) and requested:
        return granted & requested
    return granted


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


def _get_config(request: Request) -> CaliberConfig:
    config: CaliberConfig = request.app.state.config
    return config


def _config_or_none(request: Request) -> Any:
    """The app config, or ``None`` when the app is not fully wired.

    Returning ``None`` rather than raising matters: identity resolution runs on every
    request including error paths, and a half-built app must produce
    :data:`ANONYMOUS` rather than a 500.
    """
    try:
        return _get_config(request)
    except (AttributeError, KeyError, RuntimeError):
        return None
