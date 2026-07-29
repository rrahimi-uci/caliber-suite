"""Stateless CSRF token issuance + validation.

CSRF protection in CALIBER is opt-in (``CaliberConfig.csrf_enabled``)
because the common deployment shape — MLflow's auth proxy in front of
CALIBER — already protects against cross-site forgery via SameSite
cookies + same-origin enforcement at the proxy. Deployments that
*don't* have that protection (custom auth, header-based identity, a
permissive proxy config) flip the flag and CALIBER takes over.

When enabled:

1. The SPA fetches a token from ``GET /caliber/csrf`` on boot.
2. The SPA includes the token in an ``X-CALIBER-CSRF`` header on
   every state-changing request (POST / PATCH / PUT / DELETE).
3. A middleware (``CSRFMiddleware``) verifies the header on those
   methods. Failure returns 403 with a structured error.

Tokens are *stateless* — no DB, no session table. The token is::

    {unix_ts}.{hex_hmac_sha256(secret, "{ts}.{user}")}

so validation is HMAC-recompute + timestamp window check. That gives
us per-user binding (an attacker can't forge a token for someone else
without the secret) and rotation (tokens expire after
``csrf_token_ttl_seconds``).

The signing secret comes from the env var named by
``CaliberConfig.csrf_signing_secret_env`` — same pattern the webhook
dispatcher uses, so the value never lands in the resolved config
object or an audit row.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger("caliber.csrf")

_TOKEN_SEPARATOR: Final[str] = "."  # noqa: S105 — a "." separator is not a secret
_DEFAULT_TTL_SECONDS: Final[int] = 3600  # 1 hour
# Tokens accept a small skew window in case the issuer and validator
# disagree on the wall clock. 60s mirrors what most OAuth providers
# use for "not before" leniency.
_CLOCK_SKEW_SECONDS: Final[int] = 60


class CSRFValidationError(ValueError):
    """Raised by :meth:`CSRFTokenManager.validate` when a token fails.

    The middleware catches this and returns a structured 403; tests
    catch it directly to assert which failure mode fired (expired vs.
    forged vs. malformed).
    """


@dataclass(frozen=True)
class CSRFTokenManager:
    """Stateless HMAC-based token issuer + validator.

    Constructed once per app from :func:`build_token_manager`. The
    secret is held as ``bytes`` so we don't keep a string copy around.
    """

    secret: bytes
    ttl_seconds: int = _DEFAULT_TTL_SECONDS

    @property
    def is_enabled(self) -> bool:
        """True when both a secret is configured and TTL > 0.

        A zero / negative TTL is treated as "disabled" so an operator
        can't accidentally land in a mode where every token is
        immediately expired.
        """
        return bool(self.secret) and self.ttl_seconds > 0

    def issue(self, user: str, *, now: int | None = None) -> str:
        """Return a fresh token for ``user``.

        ``now`` is parameterized so tests can pin a wall-clock value
        without monkeypatching ``time``.
        """
        if not self.secret:
            raise RuntimeError("CSRFTokenManager has no signing secret; cannot issue")
        timestamp = int(time.time()) if now is None else now
        signature = self._sign(timestamp, user)
        return f"{timestamp}{_TOKEN_SEPARATOR}{signature}"

    def validate(self, token: str | None, user: str, *, now: int | None = None) -> None:
        """Raise :class:`CSRFValidationError` if the token is bad.

        Three failure modes — all surfaced as the same exception type
        but with distinct messages so the middleware's 403 response
        can include an actionable detail:

        * ``malformed`` — wrong shape (no separator, non-int timestamp).
        * ``expired``  — older than ``ttl_seconds`` (+ clock skew).
        * ``invalid_signature`` — HMAC recomputation doesn't match.
          Caught with :func:`hmac.compare_digest` for constant-time
          comparison.
        """
        if not token:
            raise CSRFValidationError("missing CSRF token")
        if _TOKEN_SEPARATOR not in token:
            raise CSRFValidationError("malformed CSRF token")
        timestamp_raw, _, signature = token.partition(_TOKEN_SEPARATOR)
        try:
            timestamp = int(timestamp_raw)
        except ValueError as exc:
            raise CSRFValidationError("malformed CSRF token") from exc

        now_int = int(time.time()) if now is None else now
        # The token is valid for [issue_ts - skew, issue_ts + ttl + skew].
        # Tokens with a *future* issue time get a small skew window so a
        # validator with a slightly slow clock doesn't reject fresh
        # tokens; anything beyond that is treated as forged.
        if timestamp > now_int + _CLOCK_SKEW_SECONDS:
            raise CSRFValidationError("invalid CSRF token (future timestamp)")
        if timestamp + self.ttl_seconds + _CLOCK_SKEW_SECONDS < now_int:
            raise CSRFValidationError("expired CSRF token")

        expected = self._sign(timestamp, user)
        if not hmac.compare_digest(expected, signature):
            raise CSRFValidationError("invalid CSRF token signature")

    def _sign(self, timestamp: int, user: str) -> str:
        signed_payload = f"{timestamp}.{user}".encode()
        return hmac.new(self.secret, signed_payload, hashlib.sha256).hexdigest()


# Sentinel for the "CSRF disabled" path — every method short-circuits
# on the empty secret so it's safe to hand out as the default.
_DISABLED_MANAGER: Final[CSRFTokenManager] = CSRFTokenManager(secret=b"", ttl_seconds=0)


class CSRFMiddleware:
    """ASGI middleware that enforces CSRF on state-changing requests.

    Activated only when :attr:`CSRFTokenManager.is_enabled` — the
    server skips installing the middleware otherwise. That way the
    runtime cost is zero in the default deployment shape.

    Enforcement rules:

    * Safe methods (GET / HEAD / OPTIONS) are passed through. CSRF is
      a write-protection concern.
    * The ``/caliber/csrf`` issuance endpoint is exempted (the SPA
      can't include a token it hasn't fetched yet).
    * Every other path requires the ``X-CALIBER-CSRF`` header. A
      missing or invalid token produces a 403 with a structured
      response body.

    Identity is resolved by delegating to :func:`caliber.auth.current_user` — session
    cookie first, then the trusted header — so tokens issued during
    auth flow A can't be replayed by user B even if a header is
    stripped or rewritten.
    """

    def __init__(
        self,
        app: object,  # ASGIApp — typed loose so the middleware can be tested without Starlette
        *,
        manager: CSRFTokenManager,
        exempt_paths: frozenset[str] = frozenset(),
        dev_user: str = "",
    ) -> None:
        self._app = app
        self._manager = manager
        self._exempt_paths = exempt_paths
        self._dev_user = _normalize_user(dev_user)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return
        if not self._manager.is_enabled:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        method = str(scope.get("method", "")).upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        path = str(scope.get("path", ""))
        if path in self._exempt_paths:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        token = _read_header(scope, b"x-caliber-csrf")
        user = self._identity(scope)
        try:
            self._manager.validate(token, user)
        except CSRFValidationError as exc:
            await _send_403(send, str(exc))
            return

        await self._app(scope, receive, send)  # type: ignore[operator]

    def _identity(self, scope: dict[str, object]) -> str:
        """Resolve the caller the **same way the routes do**, sessions included.

        This used to read ``X-CALIBER-User`` directly, and its docstring claimed that
        matched :func:`caliber.auth.current_user`. Session authentication (C1) broke
        that equivalence: in the default ``session`` mode the routes *ignore* that
        header and resolve a server-side session, so the middleware bound tokens to
        ``anonymous`` while ``/csrf`` bound them to the signed-in user — and every
        authenticated write failed with "invalid CSRF token signature".

        Delegating is what keeps the two aligned by construction rather than by comment.
        ``current_user`` caches its result on the request scope, so the handler's later
        call reuses this lookup instead of repeating it.

        Falls back to the header reader when the app is not wired far enough for
        identity resolution (a bare middleware unit test), because a CSRF check must
        never be the reason a request 500s.
        """
        try:
            from starlette.requests import Request  # noqa: PLC0415

            from caliber.auth import current_user  # noqa: PLC0415

            return _normalize_user(current_user(Request(scope)))  # type: ignore[arg-type]
        except Exception:
            return _read_user_header(scope, fallback_user=self._dev_user)


def _read_header(scope: dict[str, object], name: bytes) -> str | None:
    headers = scope.get("headers") or []
    if not isinstance(headers, list | tuple):
        return None
    for header_name, header_value in headers:
        if header_name == name:
            try:
                decoded = header_value.decode("ascii").strip()
            except (UnicodeDecodeError, AttributeError):
                return None
            return decoded or None
    return None


_ANONYMOUS_USER = "anonymous"


def _read_user_header(scope: dict[str, object], *, fallback_user: str = _ANONYMOUS_USER) -> str:
    """Return the user identity the same way :func:`caliber.auth.current_user`
    does — empty header, the literal ``anonymous`` sentinel, or a value
    containing ``,`` (a list-bypass payload) all resolve to anonymous.
    Keeping this aligned with ``caliber.auth.current_user`` matters: if
    the two disagreed, an attacker could pose as the sentinel here to
    inherit another anonymous client's CSRF token while still appearing
    authenticated to the route handler.
    """
    user = _normalize_user(_read_header(scope, b"x-caliber-user"))
    if user != _ANONYMOUS_USER:
        return user
    return fallback_user


def _normalize_user(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or raw.lower() == _ANONYMOUS_USER or "," in raw:
        return _ANONYMOUS_USER
    return raw


async def _send_403(send: object, detail: str) -> None:
    body = (
        b'{"detail":"CSRF check failed: '
        + detail.replace('"', "'").encode("utf-8")
        + b'","status_code":403}'
    )
    await send(  # type: ignore[operator]
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})  # type: ignore[operator]


def build_token_manager(
    *,
    enabled: bool,
    secret_env_var: str,
    ttl_seconds: int,
    environ: dict[str, str] | None = None,
) -> CSRFTokenManager:
    """Construct a manager from raw config + the live environment.

    The ``secret_env_var`` config field can be either a bare env-var
    name (backwards compatible) or a :mod:`caliber.secrets` URI like
    ``file:///run/secrets/csrf`` — resolution flows through
    :func:`resolve_secret` either way.

    When ``enabled=False`` returns the disabled sentinel. When enabled
    but the secret can't be resolved, logs a clear warning and returns
    disabled — the alternative is producing tokens with an empty
    secret, which would forge-protect nothing.
    """
    if not enabled:
        return _DISABLED_MANAGER
    # Lazy import: ``caliber.secrets`` is a leaf module, but routing
    # the import through ``__init__`` keeps :mod:`caliber.csrf`
    # cheap to import for callers that only need the validator types.
    from caliber.secrets import resolve_secret  # noqa: PLC0415

    secret = resolve_secret(secret_env_var, environ=environ)
    if not secret:
        logger.warning(
            "csrf is enabled but the secret source %r resolved to empty; "
            "CSRF protection will not engage. "
            "Set the source named by config.csrf_signing_secret_env.",
            secret_env_var,
        )
        return _DISABLED_MANAGER
    return CSRFTokenManager(
        secret=secret.encode("utf-8"),
        ttl_seconds=ttl_seconds,
    )
