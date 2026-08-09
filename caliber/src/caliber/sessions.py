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

Fresh local launchers intentionally retain one narrow compatibility convenience:
an empty account table may seed a server-validated ``admin/admin`` account and those
launchers separately grant it admin scope. The password is hashed at rest, the startup
log warns until it is changed, and this exception is unavailable to ordinary account
creation and password reset.

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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, NamedTuple

from sqlalchemy import select, update

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

#: Passwords below this are rejected during ordinary creation and reset. Not a
#: substitute for a real policy, but it stops weak credentials beyond the one
#: explicitly isolated local bootstrap exception.
MIN_PASSWORD_LENGTH = 12

#: Credentials normal account creation and reset must never accept, because they
#: are what an operator copying a quickstart would reach for first. The one-time
#: empty-database bootstrap explicitly handles the sole ``admin`` exception.
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

#: Requested zero-configuration local login. This credential is intentionally confined
#: to the empty-database bootstrap path; ordinary account creation and password reset keep
#: rejecting it through :data:`FORBIDDEN_PASSWORDS`.
DEFAULT_BOOTSTRAP_ADMIN_USER = "admin"
DEFAULT_BOOTSTRAP_ADMIN_PASSWORD = "admin"  # noqa: S105 - explicit local bootstrap

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


# Unknown accounts must pay exactly the same one-scrypt verification cost as a wrong
# password for a real account. Creating this sentinel inside ``authenticate`` would hash
# once and then verify once, making the unknown-user branch roughly twice as slow and
# turning the mitigation itself into a timing oracle. It is randomized once at process
# startup and is not a credential for any account.
_DUMMY_PASSWORD_HASH = hash_password("caliber-auth-timing-sentinel")


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
    allow_insecure_default: bool = False,
) -> Any:
    """Create an account. Raises :class:`AccountError` on invalid or duplicate input."""
    from caliber.db.models import CaliberUserAccount  # noqa: PLC0415

    resolved_id = validate_user_id(user_id)
    if allow_insecure_default:
        if (resolved_id, password) != (
            DEFAULT_BOOTSTRAP_ADMIN_USER,
            DEFAULT_BOOTSTRAP_ADMIN_PASSWORD,
        ):
            raise AccountError("the insecure bootstrap exception is limited to admin/admin")
    else:
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
        # Exactly one scrypt verification, matching the wrong-password branch. The
        # sentinel is built once at import; rebuilding it here would make this path 2x.
        verify_password(password or "", _DUMMY_PASSWORD_HASH)
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


# ---------------------------------------------------------------------------
# Personal access tokens (automation credentials)
# ---------------------------------------------------------------------------

#: Every PAT carries this prefix in its plaintext form. Two reasons, both
#: practical: resolution can tell a PAT from a session token without a wasted
#: database lookup, and secret scanners can match a distinctive literal rather
#: than "a long base64-ish string", which is what makes leaked-credential
#: detection possible at all.
PAT_PREFIX: Final[str] = "calpat_"


class PersonalAccessToken(NamedTuple):
    """A freshly issued token. ``token`` is returned exactly once."""

    token_id: str
    token: str


def _normalize_scopes(scopes: Iterable[str] | None) -> str:
    """Store scopes as a sorted, de-duplicated, space-separated string."""
    if not scopes:
        return ""
    return " ".join(sorted({str(scope).strip() for scope in scopes if str(scope).strip()}))


def parse_scopes(raw: str | None) -> frozenset[str]:
    return frozenset(part for part in str(raw or "").split() if part)


def create_personal_access_token(
    session: Any,
    *,
    user_id: str,
    name: str,
    scopes: Iterable[str] | None = None,
    expires_at: datetime | None = None,
    created_by: str | None = None,
    rotated_from: str | None = None,
) -> PersonalAccessToken:
    """Issue a token, returning the plaintext once and storing only its digest."""
    from caliber.db.models import CaliberPersonalAccessToken  # noqa: PLC0415
    from caliber.ids import new_personal_access_token_id  # noqa: PLC0415

    label = str(name or "").strip()
    if not label:
        raise AccountError("token name must not be empty")

    token = PAT_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    token_id = new_personal_access_token_id()
    session.add(
        CaliberPersonalAccessToken(
            token_id=token_id,
            user_id=user_id,
            name=label[:256],
            token_hash=token_fingerprint(token),
            scopes=_normalize_scopes(scopes),
            created_by=created_by or user_id,
            expires_at=expires_at,
            rotated_from=rotated_from,
        )
    )
    return PersonalAccessToken(token_id=token_id, token=token)


def resolve_personal_access_token(
    session: Any, token: str, *, now: datetime | None = None
) -> tuple[str, frozenset[str]] | None:
    """Return ``(user_id, requested_scopes)`` for a usable token, else ``None``.

    The scopes returned are what the token *asked* for, not what it gets. The
    caller intersects them with the owner's current authority, so a token can
    only ever narrow -- and a user demoted after the token was issued loses that
    authority through the token immediately.

    Rejects revoked, expired, and tokens whose account is gone or disabled, for
    the same reason :func:`resolve_session` does: those are the operations that
    make a credential boundary usable.
    """
    from caliber.db.models import (  # noqa: PLC0415
        CaliberPersonalAccessToken,
        CaliberUserAccount,
    )

    if not token or not token.startswith(PAT_PREFIX):
        return None
    now = now or datetime.now(timezone.utc)
    row = session.execute(
        select(CaliberPersonalAccessToken).where(
            CaliberPersonalAccessToken.token_hash == token_fingerprint(token)
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= now:
        return None
    # Enforce the account only when the deployment has one. ``resolve_session``
    # can require a row because a session is *created* by password login, so an
    # account necessarily exists. A PAT is issued to whatever identity the
    # deployment established -- in trusted-header mode that is a proxy-asserted
    # user with no row at all, and requiring one would make PATs work in session
    # mode only. Where a row does exist, ``disabled`` still revokes the token.
    account = session.get(CaliberUserAccount, row.user_id)
    if account is not None and account.disabled:
        return None
    return str(row.user_id), parse_scopes(row.scopes)


def touch_personal_access_token(session: Any, token: str, *, now: datetime | None = None) -> None:
    """Record last use, so an operator can retire tokens nothing calls."""
    from caliber.db.models import CaliberPersonalAccessToken  # noqa: PLC0415

    if not token or not token.startswith(PAT_PREFIX):
        return
    session.execute(
        update(CaliberPersonalAccessToken)
        .where(CaliberPersonalAccessToken.token_hash == token_fingerprint(token))
        .values(last_used_at=now or datetime.now(timezone.utc))
    )


def revoke_personal_access_token(
    session: Any, *, token_id: str, reason: str = "revoked", now: datetime | None = None
) -> bool:
    """Revoke by id. Returns whether a live token was actually revoked."""
    from caliber.db.models import CaliberPersonalAccessToken  # noqa: PLC0415

    result = session.execute(
        update(CaliberPersonalAccessToken)
        .where(
            CaliberPersonalAccessToken.token_id == token_id,
            CaliberPersonalAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=now or datetime.now(timezone.utc), revoked_reason=reason[:64])
    )
    return bool(result.rowcount)


def list_personal_access_tokens(session: Any, *, user_id: str) -> list[Any]:
    """Every token for a user, newest first. Never includes a usable secret."""
    from caliber.db.models import CaliberPersonalAccessToken  # noqa: PLC0415

    return list(
        session.execute(
            select(CaliberPersonalAccessToken)
            .where(CaliberPersonalAccessToken.user_id == user_id)
            .order_by(CaliberPersonalAccessToken.created_at.desc())
        )
        .scalars()
        .all()
    )


__all__ = [
    "DEFAULT_BOOTSTRAP_ADMIN_PASSWORD",
    "DEFAULT_BOOTSTRAP_ADMIN_USER",
    "FORBIDDEN_PASSWORDS",
    "MIN_PASSWORD_LENGTH",
    "PAT_PREFIX",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "AccountError",
    "AuthenticationError",
    "PersonalAccessToken",
    "SessionToken",
    "account_count",
    "authenticate",
    "create_account",
    "create_personal_access_token",
    "hash_password",
    "list_personal_access_tokens",
    "needs_rehash",
    "parse_scopes",
    "purge_expired_sessions",
    "resolve_personal_access_token",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_personal_access_token",
    "revoke_session",
    "set_disabled",
    "set_password",
    "token_fingerprint",
    "touch_personal_access_token",
    "touch_session",
    "validate_password",
    "validate_user_id",
    "verify_password",
]
