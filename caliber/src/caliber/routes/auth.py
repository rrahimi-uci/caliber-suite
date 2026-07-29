"""``/caliber/auth/*`` — server-validated login, logout, and session inspection.

Replaces the browser-side `admin/admin` check. The password is verified against
``caliber_user_accounts`` here, and the session token is returned as an **HttpOnly**
cookie so injected script cannot read it. The token is also returned in the body for
non-browser clients, which is why the cookie exists at all — a bearer token pasted
into JavaScript-accessible storage is the thing HttpOnly avoids.

Endpoints:

* ``POST /caliber/auth/login`` — ``{user_id, password}`` → sets the session cookie.
* ``POST /caliber/auth/logout`` — revokes the current session server-side.
* ``GET  /caliber/auth/session`` — who am I, and how is that established? The SPA
  uses this to decide whether to render a login form at all.
* ``POST /caliber/auth/accounts`` — admin-only account creation.
* ``PATCH /caliber/auth/accounts/{user_id}`` — admin-only password reset / disable.
* ``GET  /caliber/auth/accounts`` — admin-only inventory (never hashes).

Login is deliberately **rate-limited per user id and per client address**: without
it, a server-side password check is just a slower oracle. The limiter is
process-local, which is stated rather than hidden — it bounds a single instance, not
a fleet.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    AUTH_MODE_SESSION,
    SCOPE_ADMIN,
    current_scopes,
    current_user,
    require_scopes,
    session_token_from_request,
)
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.sessions import (
    AccountError,
    AuthenticationError,
    authenticate,
    create_account,
    revoke_all_sessions,
    revoke_session,
    set_disabled,
    set_password,
)

logger = logging.getLogger("caliber.routes.auth")

PREFIX = "/ajax-api/2.0/mlflow/caliber/auth"
LOGIN_PATH = PREFIX + "/login"
LOGOUT_PATH = PREFIX + "/logout"
SESSION_PATH = PREFIX + "/session"
ACCOUNTS_PATH = PREFIX + "/accounts"
ACCOUNT_DETAIL_PATH = PREFIX + "/accounts/{user_id}"

#: Failed-login budget per (user id, client address) within the window. A
#: server-side password check that can be attempted without limit is still an
#: oracle, just a slower one.
_MAX_FAILED_LOGINS = 5
_LOGIN_WINDOW_SECONDS = 300.0
_LOGIN_LOCK = threading.Lock()
_FAILED_LOGINS: dict[tuple[str, str], deque[float]] = {}
#: Bounds the tracking dict so a spray across many user ids cannot grow memory.
_MAX_TRACKED_LOGIN_KEYS = 10_000


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


def _login_blocked(user_id: str, client: str, *, now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _LOGIN_LOCK:
        window = _FAILED_LOGINS.get((user_id, client))
        if window is None:
            return False
        while window and window[0] < cutoff:
            window.popleft()
        return len(window) >= _MAX_FAILED_LOGINS


def _record_failed_login(user_id: str, client: str, *, now: float | None = None) -> None:
    now = now if now is not None else time.monotonic()
    with _LOGIN_LOCK:
        if len(_FAILED_LOGINS) > _MAX_TRACKED_LOGIN_KEYS:
            _FAILED_LOGINS.clear()
        _FAILED_LOGINS.setdefault((user_id, client), deque()).append(now)


def _clear_failed_logins(user_id: str, client: str) -> None:
    with _LOGIN_LOCK:
        _FAILED_LOGINS.pop((user_id, client), None)


def _reset_login_throttle_for_tests() -> None:
    with _LOGIN_LOCK:
        _FAILED_LOGINS.clear()


def _set_session_cookie(response: JSONResponse, config: Any, token: str, max_age: int) -> None:
    response.set_cookie(
        key=str(getattr(config, "auth_session_cookie_name", "caliber_session")),
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=bool(getattr(config, "auth_session_cookie_secure", True)),
        path="/",
    )


async def login(request: Request) -> JSONResponse:
    """Verify credentials and issue a session."""
    body = await parse_json_object(request)
    user_id = str(body.get("user_id") or "").strip()
    password = str(body.get("password") or "")
    if not user_id or not password:
        raise HTTPException(status_code=400, detail="user_id and password are required")

    config = request.app.state.config
    client = _client_key(request)
    if _login_blocked(user_id, client):
        # 429 rather than 401 so a client can distinguish "throttled" from "wrong
        # password" without the response revealing whether the account exists.
        raise HTTPException(
            status_code=429,
            detail="too many failed sign-in attempts; wait a few minutes and retry",
        )

    factory = get_session_factory(request)
    with factory() as session:
        try:
            issued = authenticate(
                session,
                user_id=user_id,
                password=password,
                ttl_seconds=float(getattr(config, "auth_session_ttl_seconds", 43200.0)),
                user_agent=request.headers.get("User-Agent"),
            )
        except AuthenticationError:
            _record_failed_login(user_id, client)
            # Committed so the failed-attempt timing work is not rolled back and so
            # a transparent hash upgrade on a *successful* login can persist.
            session.commit()
            logger.warning("failed sign-in for %r from %s", user_id, client)
            raise HTTPException(status_code=401, detail="invalid credentials") from None
        audit_record(
            session,
            actor=issued.user_id,
            action="sign_in",
            entity_type="session",
            entity_id=issued.session_id,
            details={"client": client},
        )
        session.commit()
        expires_at = issued.expires_at.isoformat()
        token = issued.token
        resolved_user = issued.user_id

    _clear_failed_logins(user_id, client)
    max_age = int(float(getattr(config, "auth_session_ttl_seconds", 43200.0)))
    response = envelope_response_dict(
        {
            "user_id": resolved_user,
            "expires_at": expires_at,
            # Returned once for non-browser clients. The cookie is what the SPA
            # uses, and it is HttpOnly precisely so the SPA never holds this value.
            "token": token,
        }
    )
    _set_session_cookie(response, config, token, max_age)
    return response


async def logout(request: Request) -> JSONResponse:
    """Revoke the current session server-side, and clear the cookie.

    Server-side revocation is the point: clearing only the cookie would leave a
    still-valid token in anything that captured it.
    """
    token = session_token_from_request(request)
    revoked = False
    if token:
        factory = get_session_factory(request)
        with factory() as session:
            revoked = revoke_session(session, token, reason="logout")
            session.commit()
    config = request.app.state.config
    response = envelope_response_dict({"revoked": revoked})
    response.delete_cookie(
        key=str(getattr(config, "auth_session_cookie_name", "caliber_session")), path="/"
    )
    return response


async def session_info(request: Request) -> JSONResponse:
    """Report who the caller is and **how** that was established.

    The ``mode`` and ``authenticated_by`` fields exist so the SPA does not have to
    guess whether to show a login form, and so an operator can see at a glance
    whether a deployment is trusting a header.
    """
    config = request.app.state.config
    mode = str(getattr(config, "auth_mode", AUTH_MODE_SESSION) or AUTH_MODE_SESSION)
    user = current_user(request)
    scopes = current_scopes(request)
    has_session = session_token_from_request(request) is not None
    if user == "anonymous":
        authenticated_by = "none"
    elif has_session:
        authenticated_by = "session"
    elif mode != AUTH_MODE_SESSION:
        authenticated_by = "trusted_header"
    else:
        authenticated_by = "dev_fallback"
    return envelope_response_dict(
        {
            "user_id": user,
            "scopes": sorted(scopes),
            "is_admin": SCOPE_ADMIN in scopes,
            "auth_mode": mode,
            "authenticated_by": authenticated_by,
            # The SPA renders a login form only when passwords are the mechanism.
            "login_required": mode == AUTH_MODE_SESSION and user == "anonymous",
        }
    )


async def list_accounts(request: Request) -> JSONResponse:
    """Admin-only account inventory. Never returns a password hash."""
    require_scopes(request, [SCOPE_ADMIN])
    from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

    factory = get_session_factory(request)
    with factory() as session:
        rows = (
            session.execute(select(CaliberUserAccount).order_by(CaliberUserAccount.user_id))
            .scalars()
            .all()
        )
        accounts = [
            {
                "user_id": row.user_id,
                "disabled": bool(row.disabled),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "password_updated_at": (
                    row.password_updated_at.isoformat() if row.password_updated_at else None
                ),
                "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None,
            }
            for row in rows
        ]
    return envelope_response_dict({"accounts": accounts, "total": len(accounts)})


async def create_account_route(request: Request) -> JSONResponse:
    """Admin-only account creation."""
    actor = require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request)
    factory = get_session_factory(request)
    with factory() as session:
        try:
            account = create_account(
                session,
                user_id=str(body.get("user_id") or ""),
                password=str(body.get("password") or ""),
                actor=actor,
            )
        except AccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="create_account",
            entity_type="account",
            entity_id=account.user_id,
            details={},
        )
        session.commit()
        user_id = account.user_id
    return envelope_response_dict({"user_id": user_id, "disabled": False}, status_code=201)


async def update_account(request: Request) -> JSONResponse:
    """Admin-only password reset and enable/disable.

    Both operations revoke the account's sessions, so they take effect immediately
    rather than whenever the current session happens to expire.
    """
    actor = require_scopes(request, [SCOPE_ADMIN])
    user_id = request.path_params["user_id"]
    body = await parse_json_object(request)
    if "password" not in body and "disabled" not in body:
        raise HTTPException(status_code=400, detail="pass 'password' and/or 'disabled'")

    factory = get_session_factory(request)
    changed: list[str] = []
    with factory() as session:
        try:
            if "password" in body:
                set_password(session, user_id=user_id, password=str(body.get("password") or ""))
                changed.append("password")
            if "disabled" in body:
                set_disabled(session, user_id=user_id, disabled=bool(body.get("disabled")))
                changed.append("disabled")
        except AccountError as exc:
            status = 404 if "does not exist" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="update_account",
            entity_type="account",
            entity_id=user_id,
            # Never the password itself, and never its hash.
            details={"changed": changed},
        )
        session.commit()
    return envelope_response_dict({"user_id": user_id, "changed": changed})


async def revoke_account_sessions(request: Request) -> JSONResponse:
    """Admin-only "sign out everywhere" for one account."""
    actor = require_scopes(request, [SCOPE_ADMIN])
    user_id = request.path_params["user_id"]
    factory = get_session_factory(request)
    with factory() as session:
        count = revoke_all_sessions(session, user_id=user_id, reason="admin_revoked")
        audit_record(
            session,
            actor=actor,
            action="revoke_sessions",
            entity_type="account",
            entity_id=user_id,
            details={"revoked": count},
        )
        session.commit()
    return envelope_response_dict({"user_id": user_id, "revoked": count})


def register(app: Starlette) -> None:
    app.routes.append(Route(LOGIN_PATH, login, methods=["POST"]))
    app.routes.append(Route(LOGOUT_PATH, logout, methods=["POST"]))
    app.routes.append(Route(SESSION_PATH, session_info, methods=["GET"]))
    app.routes.append(Route(ACCOUNTS_PATH, list_accounts, methods=["GET"]))
    app.routes.append(Route(ACCOUNTS_PATH, create_account_route, methods=["POST"]))
    app.routes.append(Route(ACCOUNT_DETAIL_PATH, update_account, methods=["PATCH"]))
    app.routes.append(
        Route(ACCOUNT_DETAIL_PATH + "/sessions", revoke_account_sessions, methods=["DELETE"])
    )
