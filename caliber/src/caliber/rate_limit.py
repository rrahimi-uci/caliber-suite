"""Per-user rate limiting middleware.

Rate limiting in CALIBER is opt-in (``CaliberConfig.rate_limit_enabled``)
because the common deployment shape — MLflow behind an auth proxy at a
gateway like NGINX, Envoy, or a corporate WAF — already handles
rate limiting upstream. Deployments without that gateway (single-node
demos, on-prem clusters with no front-door, or operators who want
defense-in-depth) flip the flag and CALIBER takes over.

The algorithm is a classic **token bucket** keyed by user identity:

* Each user gets a bucket of size ``burst`` tokens.
* Tokens refill at ``requests_per_minute / 60`` per second up to the
  bucket's capacity.
* Each request consumes one token.
* When the bucket is empty the request gets a 429 with a
  ``Retry-After`` header set to the number of seconds until the next
  token refills.

Token bucket (rather than fixed window) is the right pick here for two
reasons: it absorbs short bursts without rejecting (small-burst
friendly), and the math handles arbitrary refill granularity without
the clock-bucket-edge spike that fixed-window counters get.

State is in-process (a dict from user → ``TokenBucket``). That means
this isn't safe across multiple replicas — two replicas don't share
counters. A future enhancement swaps the bucket store for Redis when
the deployment shape warrants it; the middleware contract doesn't
change. We log a clear warning at startup if rate limiting is enabled
in a multi-replica deployment so the operator knows the limit is
per-replica, not global.

Anonymous requests (no ``X-CALIBER-User`` header) share a single
bucket named ``anonymous`` — so an unauthenticated flood from a single
client can't drown out legit traffic, and an honest "I forgot to set
the header" misconfiguration during local development gets a clear
429 with a hint to set the header.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

logger = logging.getLogger("caliber.rate_limit")

# Reserved bucket key for "no user identity present". Distinct from any
# legitimate user id (those would never be the literal string
# ``anonymous``) and stable across requests so a single anonymous client
# can't burst by churning fake identities.
_ANONYMOUS_KEY: Final[str] = "anonymous"

# Upper bound on per-replica bucket count. Long-lived single-replica
# deployments seeing many distinct principals (service accounts,
# customer-issued tokens, etc.) would otherwise grow ``_buckets``
# without bound. The LRU eviction below keeps the active set in memory
# while letting stale ones be reclaimed. 10k is comfortably larger
# than the typical operator count and small enough that the dict
# overhead stays a rounding error in process memory.
_DEFAULT_MAX_BUCKETS: Final[int] = 10_000


@dataclass
class TokenBucket:
    """Refilling token bucket for one user.

    Pure-math, time-source-parameterized. Tests pin the clock; the
    runtime passes :func:`time.monotonic`.

    Attributes
    ----------
    capacity:
        Maximum tokens the bucket can hold. Equals the configured
        ``burst`` — short bursts of up to this many requests pass
        without 429s.
    refill_per_second:
        Tokens added per second. ``requests_per_minute / 60``.
    tokens:
        Current token count. Initialized to ``capacity`` so a fresh
        user gets a full bucket on their first request.
    last_refill:
        Time the ``tokens`` field was last updated. Used to compute
        the elapsed-time refill on the next ``try_acquire`` call.
    """

    capacity: float
    refill_per_second: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        # ``last_refill`` is set by ``try_acquire`` on the first call —
        # we don't have a clock here at construction time. Sentinel:
        # use ``-inf`` so the first call always replenishes to
        # capacity, no matter when the bucket was built.
        self.last_refill = float("-inf")

    def try_acquire(self, *, now: float, cost: float = 1.0) -> bool:
        """Refill, then attempt to consume ``cost`` tokens.

        Returns
        -------
        bool
            ``True`` if the request is admitted, ``False`` if the
            bucket doesn't have enough tokens.
        """
        self._refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def seconds_until_available(self, *, now: float, cost: float = 1.0) -> float:
        """Estimate how long until ``cost`` tokens are available.

        Used to populate the ``Retry-After`` header. Returns 0 when
        enough tokens are already available (which means the caller
        should have called ``try_acquire`` first — but we're robust
        to it either way).
        """
        self._refill(now)
        deficit = cost - self.tokens
        if deficit <= 0:
            return 0.0
        if self.refill_per_second <= 0:
            # Defensive — shouldn't happen because build_limiter
            # rejects this construction. If it does, the bucket
            # never refills and the wait is unbounded; cap at the
            # capacity-refill time.
            return float("inf")
        return deficit / self.refill_per_second

    def _refill(self, now: float) -> None:
        if self.last_refill == float("-inf"):
            self.last_refill = now
            return
        elapsed = max(0.0, now - self.last_refill)
        self.last_refill = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)


def _wall_clock() -> float:
    return time.monotonic()


class RateLimiter:
    """Thread-safe collection of per-user token buckets.

    Constructed once per app via :func:`build_limiter`. Each unique
    user identity gets its own bucket lazily on first sight. The
    bucket set is **bounded** by ``max_buckets`` and evicts least-
    recently-used entries when the cap is reached — without this,
    long-lived single-replica deployments seeing many distinct
    principals would grow the dict without bound.

    The eviction is "drop the bucket from the dict" rather than
    "force-deny the user" — a user whose bucket was evicted simply
    gets a fresh full bucket on their next request. That's the
    correct semantic for an LRU cache: the eviction is invisible to
    well-behaved clients and only delays the rate limit kicking in
    by at most one burst window for the evicted principals.

    Parameters
    ----------
    requests_per_minute:
        Sustained rate. Becomes the refill rate
        (``requests_per_minute / 60``).
    burst:
        Maximum tokens a single bucket holds. The largest spike a
        user can fire off without getting a 429.
    max_buckets:
        Upper bound on the number of buckets kept in memory. When
        exceeded, the least-recently-acquired bucket is evicted.
        Defaults to :data:`_DEFAULT_MAX_BUCKETS`.
    time_source:
        Callable returning a monotonic float. Parameterized for
        tests; defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        *,
        requests_per_minute: float,
        burst: int,
        max_buckets: int = _DEFAULT_MAX_BUCKETS,
        time_source: Callable[[], float] = _wall_clock,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        if max_buckets < 1:
            raise ValueError("max_buckets must be >= 1")
        self._requests_per_minute = requests_per_minute
        self._refill_per_second = requests_per_minute / 60.0
        self._burst = burst
        self._max_buckets = max_buckets
        self._now = time_source
        self._lock = threading.Lock()
        # ``OrderedDict`` carries insertion order; we ``move_to_end``
        # on every access so the *least-recently-acquired* bucket sits
        # at the head — that's the LRU candidate for eviction.
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    @property
    def is_enabled(self) -> bool:
        # The limiter is "enabled" iff it exists — the disabled path
        # in build_limiter returns ``None`` rather than a limiter with
        # an infinite rate, so the middleware can fast-path on a
        # single ``is None`` check.
        return True

    def try_acquire(self, user: str) -> tuple[bool, float]:
        """Try to admit one request for ``user``.

        Returns
        -------
        tuple
            ``(admitted, retry_after_seconds)``. When admitted is
            ``True`` the caller proceeds; otherwise ``retry_after``
            is the number of seconds the client should wait before
            retrying.
        """
        key = user or _ANONYMOUS_KEY
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=float(self._burst),
                    refill_per_second=self._refill_per_second,
                )
                self._buckets[key] = bucket
                # Evict the LRU bucket(s) until we're back under cap.
                # ``OrderedDict.popitem(last=False)`` pops the *oldest*
                # entry — the bucket we haven't seen for the longest.
                while len(self._buckets) > self._max_buckets:
                    self._buckets.popitem(last=False)
            else:
                # Existing bucket — bump it to the most-recently-used
                # slot so the eviction order reflects access recency.
                self._buckets.move_to_end(key)
            now = self._now()
            if bucket.try_acquire(now=now):
                return True, 0.0
            return False, bucket.seconds_until_available(now=now)


class RateLimitMiddleware:
    """ASGI middleware that 429s requests exceeding the per-user budget.

    Installed only when a limiter is configured — the server skips
    installing the middleware otherwise. That way the runtime cost is
    zero in the default deployment shape.

    Identity is read from the ``X-CALIBER-User`` header the same way
    :func:`caliber.auth.current_user` does. Missing header → the
    shared ``anonymous`` bucket.

    Exempt paths bypass the limiter entirely. The default set
    includes the health endpoint (so liveness checks don't consume
    tokens) and the CSRF token issuance endpoint (so the SPA can
    bootstrap a token without spending its bucket on it).
    """

    def __init__(
        self,
        app: object,  # ASGIApp — typed loose so the middleware is testable without Starlette
        *,
        limiter: RateLimiter,
        exempt_paths: frozenset[str] = frozenset(),
        dev_user: str = "",
    ) -> None:
        self._app = app
        self._limiter = limiter
        self._exempt_paths = exempt_paths
        self._dev_user = _normalize_user(dev_user)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)  # type: ignore[operator]
            return
        path = str(scope.get("path", ""))
        if path in self._exempt_paths:
            await self._app(scope, receive, send)  # type: ignore[operator]
            return

        user = self._identity(scope)
        admitted, retry_after = self._limiter.try_acquire(user)
        if not admitted:
            await _send_429(send, user=user, retry_after_seconds=retry_after)
            return

        await self._app(scope, receive, send)  # type: ignore[operator]

    def _identity(self, scope: dict[str, object]) -> str:
        """Resolve the caller the **same way the routes do**, sessions included.

        This used to read ``X-CALIBER-User`` directly. In the shipped ``session``
        mode the routes *ignore* that header and resolve a server-side session, so
        the limiter was keying buckets on a value the authenticator does not
        trust: a caller could send a different ``X-CALIBER-User`` on every request
        and get a fresh budget each time, while still executing with their real
        signed-in authority. That does not weaken the limit, it removes it.

        :class:`caliber.csrf.CSRFMiddleware` hit the identical trap and fixed it
        the same way; delegating keeps all three (routes, CSRF, limiter) aligned
        by construction rather than by comment. ``current_user`` caches on the
        request scope, so the handler's later call reuses this lookup.

        Unauthenticated callers still collapse onto the shared ``anonymous``
        bucket, which is the existing deliberate choice: it keeps one
        unauthenticated flood from consuming an authenticated user's budget.

        Falls back to the header reader when the app is not wired far enough for
        identity resolution (a bare middleware unit test), because rate limiting
        must never be the reason a request 500s.
        """
        try:
            from starlette.requests import Request  # noqa: PLC0415

            from caliber.auth import current_user  # noqa: PLC0415

            resolved = str(current_user(Request(scope)) or "").strip()
            return resolved or _ANONYMOUS_KEY
        except Exception:
            return _read_user_header(scope, fallback_user=self._dev_user)


def _read_user_header(
    scope: Mapping[str, object],
    *,
    fallback_user: str = _ANONYMOUS_KEY,
) -> str:
    """Match ``caliber.auth.current_user`` semantics: empty/missing → anonymous."""
    headers = scope.get("headers") or []
    if not isinstance(headers, list | tuple):
        return fallback_user
    for header_name, header_value in headers:
        if header_name == b"x-caliber-user":
            try:
                decoded = header_value.decode("ascii")
            except (UnicodeDecodeError, AttributeError):
                return fallback_user
            user = _normalize_user(decoded)
            return user if user != _ANONYMOUS_KEY else fallback_user
    return fallback_user


def _normalize_user(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or raw.lower() == _ANONYMOUS_KEY or "," in raw:
        return _ANONYMOUS_KEY
    return raw


async def _send_429(send: object, *, user: str, retry_after_seconds: float) -> None:
    # Round up so a fractional second never becomes ``Retry-After: 0``
    # (which clients interpret as "retry immediately" and which would
    # uselessly burn another bucket check).
    retry_after = max(1, int(retry_after_seconds + 0.999))
    body = json.dumps(
        {
            "detail": "rate limit exceeded",
            "status_code": 429,
            "retry_after_seconds": retry_after,
            "user": user,
        }
    ).encode("utf-8")
    await send(  # type: ignore[operator]
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(retry_after).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})  # type: ignore[operator]


def build_limiter(
    *,
    enabled: bool,
    requests_per_minute: float,
    burst: int,
    max_buckets: int = _DEFAULT_MAX_BUCKETS,
) -> RateLimiter | None:
    """Construct a limiter from config, or ``None`` when disabled.

    Returning ``None`` (not an "infinite-rate" limiter) lets
    :func:`caliber.server.create_app` decide *not to install* the
    middleware in the disabled case — making the runtime cost zero
    rather than one-branch-per-request.
    """
    if not enabled:
        return None
    return RateLimiter(
        requests_per_minute=requests_per_minute,
        burst=burst,
        max_buckets=max_buckets,
    )
