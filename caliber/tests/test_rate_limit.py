"""Tests for the per-user rate limiter + its ASGI middleware.

Three layers:

1. ``TokenBucket`` — pure-math refill / acquire / wait calculations.
2. ``RateLimiter`` — per-user bucket isolation, anonymous fallback.
3. End-to-end through the live app — enable rate limiting via config,
   verify writes get 429s past the burst and `Retry-After` is set, and
   the exempt paths (health, csrf) are never throttled.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.rate_limit import (
    RateLimiter,
    TokenBucket,
    _read_user_header,
    build_limiter,
)
from caliber.server import create_app

# ---------------------------------------------------------------------------
# TokenBucket — pure math
# ---------------------------------------------------------------------------


def test_fresh_bucket_starts_full() -> None:
    """A new bucket is filled to capacity so the first call has its
    full burst budget available."""
    bucket = TokenBucket(capacity=10.0, refill_per_second=1.0)
    assert bucket.tokens == 10.0


def test_try_acquire_consumes_one_token() -> None:
    bucket = TokenBucket(capacity=3.0, refill_per_second=1.0)
    assert bucket.try_acquire(now=0.0) is True
    # On the very first call, ``_refill`` only sets ``last_refill`` —
    # tokens stay at capacity, then ``cost`` is subtracted.
    assert bucket.tokens == pytest.approx(2.0)


def test_burst_drains_then_rejects() -> None:
    """Bucket admits up to ``capacity`` requests before saying no."""
    bucket = TokenBucket(capacity=3.0, refill_per_second=1.0)
    for _ in range(3):
        assert bucket.try_acquire(now=0.0) is True
    assert bucket.try_acquire(now=0.0) is False


def test_refill_replenishes_over_time() -> None:
    """After draining, tokens come back at ``refill_per_second``."""
    bucket = TokenBucket(capacity=3.0, refill_per_second=1.0)
    for _ in range(3):
        bucket.try_acquire(now=0.0)
    # Two seconds later: 2 tokens back, so two acquires succeed and
    # the third fails.
    assert bucket.try_acquire(now=2.0) is True
    assert bucket.try_acquire(now=2.0) is True
    assert bucket.try_acquire(now=2.0) is False


def test_refill_does_not_exceed_capacity() -> None:
    """An idle bucket caps at ``capacity`` — refilling forever doesn't
    let a user bank unlimited requests."""
    bucket = TokenBucket(capacity=3.0, refill_per_second=1.0)
    # Drain so the refill has somewhere to go on the next call.
    bucket.try_acquire(now=0.0)
    bucket.try_acquire(now=0.0)
    bucket.try_acquire(now=0.0)
    # Wait far longer than capacity allows.
    bucket.try_acquire(now=10_000.0)
    # After a single acquire post-refill, the bucket cannot hold more
    # than capacity-1 tokens; it must NOT have e.g. 9999 banked.
    assert bucket.tokens <= 3.0


def test_seconds_until_available_zero_when_bucket_has_tokens() -> None:
    bucket = TokenBucket(capacity=3.0, refill_per_second=1.0)
    assert bucket.seconds_until_available(now=0.0) == 0.0


def test_seconds_until_available_proportional_to_refill() -> None:
    """When the bucket has 0 tokens, the wait is ``1 / refill_per_second``."""
    bucket = TokenBucket(capacity=3.0, refill_per_second=2.0)
    for _ in range(3):
        bucket.try_acquire(now=0.0)
    wait = bucket.seconds_until_available(now=0.0)
    assert wait == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# RateLimiter — per-user buckets
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_separate_users_have_separate_budgets() -> None:
    """Alice and Bob each get their own bucket."""
    clock = _Clock()
    limiter = RateLimiter(requests_per_minute=60.0, burst=2, time_source=clock)
    for _ in range(2):
        ok, _ = limiter.try_acquire("@alice")
        assert ok
    # Alice is out.
    ok, _ = limiter.try_acquire("@alice")
    assert ok is False
    # Bob is untouched.
    ok, _ = limiter.try_acquire("@bob")
    assert ok


def test_empty_user_id_maps_to_anonymous_bucket() -> None:
    """A missing/empty user id all routes through ``anonymous`` so a
    single client without identity can't exhaust everyone else."""
    clock = _Clock()
    limiter = RateLimiter(requests_per_minute=60.0, burst=2, time_source=clock)
    # Two distinct empty-user calls drain the SAME bucket.
    for _ in range(2):
        ok, _ = limiter.try_acquire("")
        assert ok
    ok, _ = limiter.try_acquire("")
    assert ok is False


def test_retry_after_is_nonzero_when_rejected() -> None:
    clock = _Clock()
    limiter = RateLimiter(requests_per_minute=60.0, burst=1, time_source=clock)
    ok, _ = limiter.try_acquire("@alice")
    assert ok
    ok, retry_after = limiter.try_acquire("@alice")
    assert ok is False
    assert retry_after > 0


def test_invalid_construction_rejected() -> None:
    with pytest.raises(ValueError, match="requests_per_minute"):
        RateLimiter(requests_per_minute=0, burst=1)
    with pytest.raises(ValueError, match="burst"):
        RateLimiter(requests_per_minute=60, burst=0)
    with pytest.raises(ValueError, match="max_buckets"):
        RateLimiter(requests_per_minute=60, burst=1, max_buckets=0)


def test_lru_eviction_caps_bucket_count() -> None:
    """Per-user bucket dict is bounded — long-lived single-replica
    deployments seeing many distinct principals (high-cardinality
    service accounts, customer-issued tokens) don't grow memory
    without limit. Eviction is LRU so an active user never loses
    their bucket while idle ones are reclaimed."""
    clock = _Clock()
    limiter = RateLimiter(
        requests_per_minute=60.0,
        burst=10,
        max_buckets=3,
        time_source=clock,
    )

    # Touch four distinct principals; capacity is 3 → oldest evicted.
    for user in ("@alice", "@bob", "@carol", "@dave"):
        ok, _ = limiter.try_acquire(user)
        assert ok

    assert len(limiter._buckets) == 3
    assert "@alice" not in limiter._buckets  # oldest, evicted
    assert {"@bob", "@carol", "@dave"} <= set(limiter._buckets.keys())


def test_lru_access_refreshes_recency() -> None:
    """Touching an existing bucket bumps it to most-recently-used so
    a steady-state stream from one user doesn't get evicted by a
    burst of new identities."""
    clock = _Clock()
    limiter = RateLimiter(
        requests_per_minute=60.0,
        burst=10,
        max_buckets=3,
        time_source=clock,
    )
    limiter.try_acquire("@alice")
    limiter.try_acquire("@bob")
    limiter.try_acquire("@carol")
    # Re-touch alice — now alice is MRU, bob is LRU.
    limiter.try_acquire("@alice")
    # Adding a fourth evicts bob (the new LRU), not alice.
    limiter.try_acquire("@dave")
    assert "@alice" in limiter._buckets
    assert "@bob" not in limiter._buckets
    assert {"@alice", "@carol", "@dave"} <= set(limiter._buckets.keys())


def test_lru_evicted_user_gets_fresh_full_bucket() -> None:
    """Eviction is invisible to the principal — their next request
    sees a full bucket again (the limit just took a one-burst-window
    delay to kick in)."""
    clock = _Clock()
    limiter = RateLimiter(
        requests_per_minute=60.0,
        burst=2,
        max_buckets=2,
        time_source=clock,
    )

    # Drain alice's bucket.
    assert limiter.try_acquire("@alice") == (True, 0.0)
    assert limiter.try_acquire("@alice") == (True, 0.0)
    ok, _ = limiter.try_acquire("@alice")
    assert ok is False

    # Push alice out via two unrelated principals.
    limiter.try_acquire("@bob")
    limiter.try_acquire("@carol")
    assert "@alice" not in limiter._buckets

    # Alice comes back — she gets a fresh full bucket.
    ok, _ = limiter.try_acquire("@alice")
    assert ok


# ---------------------------------------------------------------------------
# build_limiter
# ---------------------------------------------------------------------------


def test_build_limiter_disabled_returns_none() -> None:
    """The disabled path returns ``None`` rather than an infinite-rate
    limiter — the server uses this to decide *not to install* the
    middleware at all."""
    assert build_limiter(enabled=False, requests_per_minute=60, burst=10) is None


def test_build_limiter_enabled_returns_limiter() -> None:
    limiter = build_limiter(enabled=True, requests_per_minute=60, burst=10)
    assert limiter is not None


def test_middleware_identity_uses_dev_user_fallback() -> None:
    assert _read_user_header({"headers": []}, fallback_user="@local-admin") == "@local-admin"
    assert (
        _read_user_header(
            {"headers": [(b"x-caliber-user", b"anonymous")]},
            fallback_user="@local-admin",
        )
        == "@local-admin"
    )


# ---------------------------------------------------------------------------
# End-to-end through the live app
# ---------------------------------------------------------------------------


def _build_rate_client(
    *,
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    burst: int,
    requests_per_minute: float = 60.0,
    enabled: bool = True,
) -> TestClient:
    """Build a TestClient with rate limiting wired through real config."""
    db_path = tmp_path / "caliber-rate.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("CALIBER_ADMIN_USERS", "@admin")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CALIBER_RATE_LIMIT_BURST", str(burst))
    monkeypatch.setenv("CALIBER_RATE_LIMIT_REQUESTS_PER_MINUTE", str(requests_per_minute))

    config = CaliberConfig.load()
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    return TestClient(app, headers={"X-CALIBER-User": "@admin"})


@pytest.fixture
def small_burst_client(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """3-request burst, very slow refill (1/min) so the limit trips quickly."""
    client = _build_rate_client(
        tmp_path=tmp_path,
        engine=engine,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
        burst=3,
        requests_per_minute=1.0,
    )
    with client:
        yield client


def test_within_burst_passes(small_burst_client: TestClient) -> None:
    """The first ``burst`` requests succeed."""
    for _ in range(3):
        response = small_burst_client.get("/ajax-api/2.0/mlflow/caliber/agents")
        assert response.status_code == 200


def test_exceeding_burst_returns_429(small_burst_client: TestClient) -> None:
    """The (burst+1)th request gets a 429 with Retry-After."""
    for _ in range(3):
        small_burst_client.get("/ajax-api/2.0/mlflow/caliber/agents")
    response = small_burst_client.get("/ajax-api/2.0/mlflow/caliber/agents")
    assert response.status_code == 429
    body = response.json()
    assert body["status_code"] == 429
    assert "rate limit" in body["detail"].lower()
    assert response.headers.get("retry-after") is not None
    assert int(response.headers["retry-after"]) >= 1
    assert body["retry_after_seconds"] >= 1


def test_separate_users_have_separate_budgets_e2e(small_burst_client: TestClient) -> None:
    """An exhausted @admin doesn't lock out @other."""
    for _ in range(3):
        small_burst_client.get("/ajax-api/2.0/mlflow/caliber/agents")
    rejected = small_burst_client.get("/ajax-api/2.0/mlflow/caliber/agents")
    assert rejected.status_code == 429

    response = small_burst_client.get(
        "/ajax-api/2.0/mlflow/caliber/agents",
        headers={"X-CALIBER-User": "@other"},
    )
    # ``@other`` is anonymous to the auth layer but the rate limiter
    # gives them their own bucket keyed by the header value.
    assert response.status_code in (200, 401)
    # If it failed it must not be a 429 — that would mean the user
    # isolation is broken.
    assert response.status_code != 429


def test_health_endpoint_is_exempt(small_burst_client: TestClient) -> None:
    """Health probes don't consume tokens — operators can poll it
    aggressively from a single process without exhausting the bucket."""
    for _ in range(20):
        response = small_burst_client.get("/ajax-api/2.0/mlflow/caliber/health")
        assert response.status_code == 200


def test_csrf_endpoint_is_exempt(small_burst_client: TestClient) -> None:
    """The CSRF issuance endpoint is exempt so the SPA can bootstrap
    a token without spending its budget on it."""
    for _ in range(10):
        response = small_burst_client.get("/ajax-api/2.0/mlflow/caliber/csrf")
        assert response.status_code == 200


def test_rate_limiting_disabled_does_not_install_middleware(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature is disabled the limiter is None on app.state
    and a flood of requests passes cleanly."""
    client = _build_rate_client(
        tmp_path=tmp_path,
        engine=engine,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
        burst=1,
        enabled=False,
    )
    with client:
        assert client.app.state.rate_limiter is None  # type: ignore[union-attr]
        for _ in range(10):
            response = client.get("/ajax-api/2.0/mlflow/caliber/agents")
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# Bucket identity cannot be chosen by the caller
# ---------------------------------------------------------------------------


def test_rotating_the_identity_header_cannot_mint_fresh_budgets(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller must not be able to pick their own bucket.

    In the shipped ``session`` auth mode the routes ignore ``X-CALIBER-User``
    and resolve a server-side session, but the limiter used to key buckets on
    that header. Sending a different value per request therefore produced a
    fresh full bucket every time — the limit was not weakened, it was absent.
    The limiter now resolves identity the same way the routes do, so all of
    these collapse onto one bucket and the burst is enforced.
    """
    monkeypatch.setenv("CALIBER_AUTH_MODE", "session")
    client = _build_rate_client(
        tmp_path=tmp_path,
        engine=engine,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
        burst=3,
        requests_per_minute=1.0,
    )
    with client:
        statuses = [
            client.get(
                "/ajax-api/2.0/mlflow/caliber/workflows",
                headers={"X-CALIBER-User": f"@rotating-{index}"},
            ).status_code
            for index in range(12)
        ]

    # Every request presented a distinct identity. Before the fix all 12 were
    # admitted; now the shared bucket runs dry and the limiter engages.
    assert 429 in statuses, f"header rotation still mints fresh budgets: {statuses}"
