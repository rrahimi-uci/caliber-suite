"""Server-validated password accounts and revocable sessions.

Closes C1. The prior boundary was not a boundary: the SPA validated
``admin/admin`` in the browser, sent the resulting identity as ``X-CALIBER-User``,
and the backend trusted it — so any client could assert any identity, and with the
shipped Compose defaults a request with *no* header was an admin.

Three separate defects, fixed separately:

1. **No server-side credential check.** Accounts now live in
   ``caliber_user_accounts`` with a scrypt password hash. Login verifies the
   password server-side and issues a session; nothing about the identity is
   client-asserted.
2. **Sessions must be revocable.** A stateless signed token cannot be revoked
   before it expires, which makes "disable this account now" impossible. Sessions
   are therefore rows in ``caliber_sessions``; only a SHA-256 hash of the token is
   stored, so a database read does not yield usable credentials.
3. **Header trust must be explicit and bounded.** ``X-CALIBER-User`` is honoured
   only in ``trusted_header`` mode, which is opt-in, and that mode can additionally
   require a shared proxy secret so a request that bypasses the proxy is rejected.
   The no-header dev fallback is off by default.

Why stdlib scrypt rather than argon2/bcrypt: it is memory-hard, it is in
:mod:`hashlib`, and adding a password-hashing dependency to a control plane is a
supply-chain decision that should not be made incidentally. Parameters are stored
*with* each hash so they can be raised later without invalidating existing
passwords.

This module deliberately contains no HTTP: :mod:`caliber.routes.auth` owns the
endpoints and cookie handling, and :mod:`caliber.auth` owns resolution order. Keeping
them apart is what lets the credential rules be tested without a request.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

logger = logging.getLogger("caliber.sessions")

#: scrypt work factors. ``n=2**15`` with ``r=8, p=1`` costs ~32 MB and tens of
#: milliseconds — high enough to make offline cracking expensive, low enough that a
#: login is not a denial-of-service vector against the control plane.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
_SALT_BYTES = 16

#: Session tokens: 32 bytes of ``secrets`` entropy. Long enough that online
#: guessing is hopeless, short enough for a cookie.
_TOKEN_BYTES = 32

#: Passwords below this are rejected at creation. Not a substitute for a real
#: policy, but it stops the specific thing this replaces: a four-character
#: well-known default.
MIN_PASSWORD_LENGTH = 12

#: Credentials that must never be accepted, because they are what the demo login
#: used and what an operator copying a quickstart would reach for first.
FORBIDDEN_PASSWORDS = frozenset(
    {
        "admin",
        "admin123",
        "administrator",
        "caliber",
        "caliber123",
        "changeme",
        "password",
        "password123",
        "letmein",
        "secret",
    }
)

#: A user id is an opaque handle used as a resource ``owner`` value and parsed out
#: of comma-separated scope lists, so a comma or whitespace in one would corrupt
#: authorization. Rejected at creation rather than sanitized at read time.
USER_ID_RE = re.compile(r"^[A-Za-z0-9@._\-]{1,256}$")


class AccountError(ValueError):
    """Invalid account input — bad user id, weak password, duplicate account."""


class AuthenticationError(Exception):
    """Credentials did not verify, or the account cannot be used.

    Deliberately carries no detail about *which* of those it was: distinguishing
    "no such user" from "wrong password" turns the login endpoint into a user
    enumeration oracle.
    """


@dataclass(frozen=True)
class SessionToken:
    """A freshly issued session. ``token`` is returned once and never stored."""

    token: str
    session_id: str
    user_id: str
    expires_at: datetime


def hash_password(password: str) -> str:
    """Hash a password into a self-describing ``scrypt$n$r$p$salt$hash`` string.

    The parameters travel with the hash so they can be raised later without
    invalidating existing passwords — a hash stored under weaker parameters still
    verifies, and :func:`needs_rehash` reports that it should be upgraded.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_N * SCRYPT_R * 200,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Verify a password against a stored hash in constant time.

    A malformed stored hash returns ``False`` rather than raising: a corrupted row
    must fail the login, not 500 the endpoint.
    """
    try:
        scheme, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=int(n) * int(r) * 200,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived.hex(), hash_hex)


def needs_rehash(encoded: str) -> bool:
    """Whether a stored hash uses weaker parameters than the current policy."""
    try:
        scheme, n, r, p, _salt, _hash = encoded.split("$")
    except ValueError:
        return True
    return scheme != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def validate_user_id(user_id: str) -> str:
    value = (user_id or "").strip()
    if not USER_ID_RE.match(value):
        raise AccountError(
            "user id must be 1-256 characters of letters, digits, '@', '.', '_', or '-' "
            "(a comma would corrupt the scope lists it is matched against)"
        )
    return value


def validate_password(password: str) -> str:
    value = password or ""
    if len(value) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if value.strip().casefold() in FORBIDDEN_PASSWORDS:
        raise AccountError(
            "that password is on the rejected list; it is one of the well-known "
            "defaults this login replaces"
        )
    return value


def token_fingerprint(token: str) -> str:
    """SHA-256 of a session token. Only this is stored.

    A database read therefore does not yield a usable session, which is the whole
    point of hashing something that is already high-entropy.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


def create_account(
    session: Any,
    *,
    user_id: str,
    password: str,
    actor: str = "system",
) -> Any:
    """Create an account. Raises :class:`AccountError` on invalid or duplicate input."""
    from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

    resolved_id = validate_user_id(user_id)
    validate_password(password)
    if session.get(CaliberUserAccount, resolved_id) is not None:
        raise AccountError(f"account {resolved_id!r} already exists")
    now = datetime.now(timezone.utc)
    account = CaliberUserAccount(
        user_id=resolved_id,
        password_hash=hash_password(password),
        disabled=False,
        created_by=actor,
        created_at=now,
        password_updated_at=now,
    )
    session.add(account)
    session.flush()
    return account


def set_password(session: Any, *, user_id: str, password: str) -> None:
    """Replace an account's password and revoke every existing session.

    Revocation is the point: a password change that leaves old sessions valid does
    not actually lock anyone out, which is the reason people change passwords.
    """
    from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

    account = session.get(CaliberUserAccount, user_id)
    if account is None:
        raise AccountError(f"account {user_id!r} does not exist")
    validate_password(password)
    account.password_hash = hash_password(password)
    account.password_updated_at = datetime.now(timezone.utc)
    revoke_all_sessions(session, user_id=user_id, reason="password_changed")
    session.flush()


def set_disabled(session: Any, *, user_id: str, disabled: bool) -> None:
    """Enable or disable an account, revoking its sessions when disabling.

    Without the revocation, disabling an account would take effect only when its
    current session happened to expire.
    """
    from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

    account = session.get(CaliberUserAccount, user_id)
    if account is None:
        raise AccountError(f"account {user_id!r} does not exist")
    account.disabled = bool(disabled)
    if disabled:
        revoke_all_sessions(session, user_id=user_id, reason="account_disabled")
    session.flush()


def account_count(session: Any) -> int:
    from sqlalchemy import func  # noqa: PLC0415

    from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

    return int(session.execute(select(func.count()).select_from(CaliberUserAccount)).scalar() or 0)


# ---------------------------------------------------------------------------
# Login and session lifecycle
# ---------------------------------------------------------------------------


def authenticate(
    session: Any,
    *,
    user_id: str,
    password: str,
    ttl_seconds: float,
    user_agent: str | None = None,
) -> SessionToken:
    """Verify credentials and issue a session, or raise :class:`AuthenticationError`.

    An unknown user still pays the cost of a hash verification against a dummy
    value, so response timing does not reveal whether the account exists.
    """
    from caliber.db.models import CaliberSession, CaliberUserAccount  # noqa: PLC0415

    account = session.get(CaliberUserAccount, (user_id or "").strip())
    if account is None:
        # Constant-ish work for an unknown user: verify against a throwaway hash
        # so login timing is not an enumeration oracle.
        verify_password(password or "", hash_password("dummy-value-for-timing"))
        raise AuthenticationError("invalid credentials")
    if account.disabled:
        verify_password(password or "", account.password_hash)
        raise AuthenticationError("invalid credentials")
    if not verify_password(password or "", account.password_hash):
        raise AuthenticationError("invalid credentials")

    if needs_rehash(account.password_hash):
        # Transparent upgrade on a successful login: the operator never has to
        # migrate hashes by hand when the parameters are raised.
        account.password_hash = hash_password(password)

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(ttl_seconds, 60.0))
    session_id = "sess-" + secrets.token_hex(16)
    session.add(
        CaliberSession(
            session_id=session_id,
            user_id=account.user_id,
            token_hash=token_fingerprint(token),
            created_at=now,
            expires_at=expires_at,
            last_seen_at=now,
            user_agent=(user_agent or "")[:256] or None,
        )
    )
    account.last_login_at = now
    session.flush()
    return SessionToken(
        token=token, session_id=session_id, user_id=account.user_id, expires_at=expires_at
    )


def resolve_session(session: Any, token: str, *, now: datetime | None = None) -> str | None:
    """Return the user id for a valid session token, else ``None``.

    Rejects a revoked session, an expired session, and a session whose account has
    since been disabled or deleted — the last two are why this is a database lookup
    rather than signature verification.
    """
    from caliber.db.models import CaliberSession, CaliberUserAccount  # noqa: PLC0415

    if not token:
        return None
    now = now or datetime.now(timezone.utc)
    row = session.execute(
        select(CaliberSession).where(CaliberSession.token_hash == token_fingerprint(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= now:
        return None
    account = session.get(CaliberUserAccount, row.user_id)
    if account is None or account.disabled:
        return None
    return str(row.user_id)


def touch_session(session: Any, token: str, *, now: datetime | None = None) -> None:
    """Record last-seen for an active session. Best-effort; never raises."""
    from caliber.db.models import CaliberSession  # noqa: PLC0415

    try:
        row = session.execute(
            select(CaliberSession).where(CaliberSession.token_hash == token_fingerprint(token))
        ).scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.last_seen_at = now or datetime.now(timezone.utc)
    except Exception:
        logger.debug("could not update session last_seen_at", exc_info=True)


def revoke_session(session: Any, token: str, *, reason: str = "logout") -> bool:
    """Revoke one session by token. Returns whether anything was revoked."""
    from caliber.db.models import CaliberSession  # noqa: PLC0415

    row = session.execute(
        select(CaliberSession).where(CaliberSession.token_hash == token_fingerprint(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    row.revoked_reason = reason
    session.flush()
    return True


def revoke_all_sessions(session: Any, *, user_id: str, reason: str) -> int:
    """Revoke every active session for a user. Returns the count revoked."""
    from caliber.db.models import CaliberSession  # noqa: PLC0415

    rows = (
        session.execute(
            select(CaliberSession).where(
                CaliberSession.user_id == user_id,
                CaliberSession.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    session.flush()
    return len(rows)


def purge_expired_sessions(session: Any, *, now: datetime | None = None) -> int:
    """Delete sessions that expired more than a day ago.

    Retaining them briefly keeps `last_seen_at` useful for an operator asking "was
    this account active?"; retaining them forever grows a table nothing reads.
    """
    from caliber.db.models import CaliberSession  # noqa: PLC0415

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=1)
    rows = (
        session.execute(
            select(CaliberSession).where(CaliberSession.expires_at < cutoff.replace(tzinfo=None))
        )
        .scalars()
        .all()
    )
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


__all__ = [
    "FORBIDDEN_PASSWORDS",
    "MIN_PASSWORD_LENGTH",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "AccountError",
    "AuthenticationError",
    "SessionToken",
    "account_count",
    "authenticate",
    "create_account",
    "hash_password",
    "needs_rehash",
    "purge_expired_sessions",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "set_disabled",
    "set_password",
    "token_fingerprint",
    "touch_session",
    "validate_password",
    "validate_user_id",
    "verify_password",
]
