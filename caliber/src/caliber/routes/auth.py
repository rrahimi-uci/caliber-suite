"""``/caliber/auth/*`` — server-validated login, logout, and session inspection.

Replaces the browser-side `admin/admin` check. The password is verified against
``caliber_user_accounts`` here, and the session token is returned only as an
**HttpOnly** cookie so injected script cannot read it. Returning the same bearer in
JSON would defeat that boundary.

Endpoints:

* ``POST /caliber/auth/login`` — ``{user_id, password}`` → sets the session cookie.
* ``POST /caliber/auth/logout`` — revokes the current session server-side.
* ``GET  /caliber/auth/session`` — who am I, and how is that established? The SPA
  uses this to decide whether to render a login form at all.
* ``POST /caliber/auth/accounts`` — admin-only account creation.
* ``PATCH /caliber/auth/accounts/{user_id}`` — admin-only password reset / disable.
* ``GET  /caliber/auth/accounts`` — admin-only inventory (never hashes).

Login is deliberately rate-limited both per ``(user id, client address)`` and per
client across user IDs: without the second budget, an attacker can spray a new name on
every request and never reach the first. The limiter is process-local, which is stated
rather than hidden — it bounds a single instance, not a fleet.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    ALL_SCOPES,
    AUTH_MODE_SESSION,
    SCOPE_ADMIN,
    current_scopes,
    current_user,
    require_scopes,
    require_user,
    session_token_from_request,
)
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    parse_json_object,
)
from caliber.schemas import (
    AccountListSchema,
    AccountMutationSchema,
    AccountSchema,
    IssuedPersonalAccessTokenSchema,
    LoginSchema,
    LogoutSchema,
    PersonalAccessTokenListSchema,
    PersonalAccessTokenSchema,
    SessionInfoSchema,
    TokenRevocationSchema,
)
from caliber.sessions import (
    AccountError,
    AuthenticationError,
    authenticate,
    create_account,
    create_personal_access_token,
    list_personal_access_tokens,
    parse_scopes,
    revoke_all_sessions,
    revoke_personal_access_token,
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
TOKENS_PATH = PREFIX + "/tokens"
TOKEN_DETAIL_PATH = PREFIX + "/tokens/{token_id}"
TOKEN_ROTATE_PATH = PREFIX + "/tokens/{token_id}/rotate"

#: Failed-login budget per (user id, client address) within the window. A
#: server-side password check that can be attempted without limit is still an
#: oracle, just a slower one.
_MAX_FAILED_LOGINS = 5
# A client-wide ceiling prevents username spray from creating a fresh five-attempt
# budget for every guessed account while leaving room for several users behind one NAT.
_MAX_FAILED_LOGINS_PER_CLIENT = 20
_LOGIN_WINDOW_SECONDS = 300.0
_LOGIN_LOCK = threading.Lock()
_FAILED_LOGINS: dict[tuple[str, str], deque[float]] = {}
_ALL_USERS = "*"
#: Bounds the tracking dict so a spray across many user ids cannot grow memory.
_MAX_TRACKED_LOGIN_KEYS = 10_000


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


def _login_blocked(user_id: str, client: str, *, now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _LOGIN_LOCK:
        user_window = _FAILED_LOGINS.get((user_id, client))
        client_window = _FAILED_LOGINS.get((_ALL_USERS, client))
        for window in (user_window, client_window):
            if window is not None:
                while window and window[0] < cutoff:
                    window.popleft()
        return bool(
            (user_window is not None and len(user_window) >= _MAX_FAILED_LOGINS)
            or (client_window is not None and len(client_window) >= _MAX_FAILED_LOGINS_PER_CLIENT)
        )


def _record_failed_login(user_id: str, client: str, *, now: float | None = None) -> None:
    now = now if now is not None else time.monotonic()
    with _LOGIN_LOCK:
        if len(_FAILED_LOGINS) > _MAX_TRACKED_LOGIN_KEYS:
            _FAILED_LOGINS.clear()
        _FAILED_LOGINS.setdefault((user_id, client), deque()).append(now)
        _FAILED_LOGINS.setdefault((_ALL_USERS, client), deque()).append(now)


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
        resolved_user = issued.user_id

    _clear_failed_logins(user_id, client)
    max_age = int(float(getattr(config, "auth_session_ttl_seconds", 43200.0)))
    response = envelope_response(LoginSchema(user_id=resolved_user, expires_at=expires_at))
    _set_session_cookie(response, config, issued.token, max_age)
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
    response = envelope_response(LogoutSchema(revoked=revoked))
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
    return envelope_response(
        SessionInfoSchema(
            user_id=user,
            scopes=sorted(scopes),
            is_admin=SCOPE_ADMIN in scopes,
            auth_mode=mode,
            authenticated_by=authenticated_by,
            # The SPA renders a login form only when passwords are the mechanism.
            login_required=mode == AUTH_MODE_SESSION and user == "anonymous",
        )
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
    return envelope_response(
        AccountListSchema(
            accounts=[AccountSchema.model_validate(a) for a in accounts], total=len(accounts)
        )
    )


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
    return envelope_response(
        AccountMutationSchema(user_id=user_id, disabled=False), status_code=201
    )


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
    return envelope_response(AccountMutationSchema(user_id=user_id, changed=changed))


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
    return envelope_response(AccountMutationSchema(user_id=user_id, revoked=count))


# ---------------------------------------------------------------------------
# Personal access tokens
# ---------------------------------------------------------------------------


def _token_view(row: Any) -> dict[str, Any]:
    """Serialize a token *without* its secret, which exists only at issuance."""
    return {
        "token_id": row.token_id,
        "user_id": row.user_id,
        "name": row.name,
        "scopes": sorted(parse_scopes(row.scopes)),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_reason": row.revoked_reason,
        "rotated_from": row.rotated_from,
        "active": row.revoked_at is None,
    }


def _requested_expiry(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("expires_at")
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid expires_at: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="expires_at must be in the future")
    return parsed


def _validated_scopes(payload: dict[str, Any], request: Request) -> list[str]:
    """Reject a ceiling the caller could not grant, and unknown scope names.

    Refusing up front is friendlier than silently issuing a token whose extra
    scopes are intersected away at every request -- the operator would see a
    token that claims authority it never exercises.
    """
    requested = payload.get("scopes") or []
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="scopes must be a list of strings")
    scopes = {str(item).strip() for item in requested if str(item).strip()}
    unknown = sorted(scopes - set(ALL_SCOPES))
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown scopes: {unknown}")
    holder = current_scopes(request)
    excessive = sorted(scopes - holder)
    if excessive:
        raise HTTPException(
            status_code=403,
            detail=f"cannot grant scopes you do not hold: {excessive}",
        )
    return sorted(scopes)


# The four token operations below run their database work in module-level
# synchronous helpers dispatched through ``run_in_threadpool``. Two reasons:
# the route layer is async while SQLAlchemy here is not, so an inline session
# holds the event loop for the query's duration; and the offload ratchet in
# tests/test_async_offload_ratchet.py matches on the *async function's* source
# text, so a nested helper would still be counted as inline.


def _load_tokens(factory: Any, *, actor: str) -> list[dict[str, Any]]:
    with factory() as session:
        return [_token_view(row) for row in list_personal_access_tokens(session, user_id=actor)]


def _create_token(
    factory: Any,
    *,
    actor: str,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
) -> tuple[dict[str, Any], str]:
    with factory() as session:
        try:
            issued = create_personal_access_token(
                session,
                user_id=actor,
                name=name,
                scopes=scopes,
                expires_at=expires_at,
                created_by=actor,
            )
        except AccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="create_personal_access_token",
            entity_type="personal_access_token",
            entity_id=issued.token_id,
            # The secret is deliberately absent: an audit row is read by more
            # people than the issuing response, and retained far longer.
            details={
                "scopes": scopes,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        session.commit()
        return _token_view(session.get(_PatModel(), issued.token_id)), issued.token


def _revoke_token(factory: Any, *, actor: str, token_id: str) -> bool:
    with factory() as session:
        row = session.get(_PatModel(), token_id)
        # 404 rather than 403 for someone else's token: confirming existence
        # would let any user enumerate the token ids of every other user.
        if row is None or row.user_id != actor:
            raise HTTPException(status_code=404, detail=f"token {token_id!r} not found")
        revoked = revoke_personal_access_token(session, token_id=token_id, reason="user_revoked")
        audit_record(
            session,
            actor=actor,
            action="revoke_personal_access_token",
            entity_type="personal_access_token",
            entity_id=token_id,
            details={"already_revoked": not revoked},
        )
        session.commit()
        return revoked


def _rotate_token(factory: Any, *, actor: str, token_id: str) -> tuple[dict[str, Any], str]:
    with factory() as session:
        row = session.get(_PatModel(), token_id)
        if row is None or row.user_id != actor:
            raise HTTPException(status_code=404, detail=f"token {token_id!r} not found")
        if row.revoked_at is not None:
            raise HTTPException(status_code=409, detail="cannot rotate a revoked token")
        issued = create_personal_access_token(
            session,
            user_id=actor,
            name=row.name,
            scopes=parse_scopes(row.scopes),
            expires_at=row.expires_at,
            created_by=actor,
            rotated_from=token_id,
        )
        revoke_personal_access_token(session, token_id=token_id, reason="rotated")
        audit_record(
            session,
            actor=actor,
            action="rotate_personal_access_token",
            entity_type="personal_access_token",
            entity_id=issued.token_id,
            details={"rotated_from": token_id},
        )
        session.commit()
        return _token_view(session.get(_PatModel(), issued.token_id)), issued.token


async def list_tokens(request: Request) -> JSONResponse:
    """Every token belonging to the caller. Never returns a usable secret."""
    actor = require_user(request)
    tokens = await run_in_threadpool(_load_tokens, get_session_factory(request), actor=actor)
    return envelope_response(
        PersonalAccessTokenListSchema(
            tokens=[PersonalAccessTokenSchema.model_validate(t) for t in tokens]
        )
    )


async def create_token(request: Request) -> JSONResponse:
    """Issue a token for the caller. The plaintext is returned exactly once."""
    actor = require_user(request)
    payload = await parse_json_object(request)
    scopes = _validated_scopes(payload, request)
    expires_at = _requested_expiry(payload)
    view, secret = await run_in_threadpool(
        _create_token,
        get_session_factory(request),
        actor=actor,
        name=str(payload.get("name") or ""),
        scopes=scopes,
        expires_at=expires_at,
    )
    view["token"] = secret
    return envelope_response(IssuedPersonalAccessTokenSchema.model_validate(view), status_code=201)


async def revoke_token(request: Request) -> JSONResponse:
    """Revoke one of the caller's tokens."""
    actor = require_user(request)
    token_id = request.path_params["token_id"]
    revoked = await run_in_threadpool(
        _revoke_token, get_session_factory(request), actor=actor, token_id=token_id
    )
    return envelope_response(TokenRevocationSchema(token_id=token_id, revoked=revoked))


async def rotate_token(request: Request) -> JSONResponse:
    """Replace a token with a new secret, preserving name and scopes.

    Rotation is one transaction: the old token is revoked and the replacement
    issued together, so a failure cannot leave an account with two live tokens
    or none. ``rotated_from`` links them, so the audit trail reads as a rotation
    rather than an unexplained revoke next to an unexplained create.
    """
    actor = require_user(request)
    token_id = request.path_params["token_id"]
    view, secret = await run_in_threadpool(
        _rotate_token, get_session_factory(request), actor=actor, token_id=token_id
    )
    view["token"] = secret
    return envelope_response(IssuedPersonalAccessTokenSchema.model_validate(view), status_code=201)


def _PatModel() -> Any:  # noqa: N802
    from caliber.db.models import CaliberPersonalAccessToken  # noqa: PLC0415

    return CaliberPersonalAccessToken


def register(app: Starlette) -> None:
    app.routes.append(Route(TOKENS_PATH, list_tokens, methods=["GET"]))
    app.routes.append(Route(TOKENS_PATH, create_token, methods=["POST"]))
    app.routes.append(Route(TOKEN_DETAIL_PATH, revoke_token, methods=["DELETE"]))
    app.routes.append(Route(TOKEN_ROTATE_PATH, rotate_token, methods=["POST"]))
    app.routes.append(Route(LOGIN_PATH, login, methods=["POST"]))
    app.routes.append(Route(LOGOUT_PATH, logout, methods=["POST"]))
    app.routes.append(Route(SESSION_PATH, session_info, methods=["GET"]))
    app.routes.append(Route(ACCOUNTS_PATH, list_accounts, methods=["GET"]))
    app.routes.append(Route(ACCOUNTS_PATH, create_account_route, methods=["POST"]))
    app.routes.append(Route(ACCOUNT_DETAIL_PATH, update_account, methods=["PATCH"]))
    app.routes.append(
        Route(ACCOUNT_DETAIL_PATH + "/sessions", revoke_account_sessions, methods=["DELETE"])
    )
