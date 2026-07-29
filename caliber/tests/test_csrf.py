"""Tests for the stateless CSRF token manager + middleware.

Three layers:

1. ``CSRFTokenManager.issue`` / ``validate`` — pure HMAC math: signed
   payload format, user binding, TTL window, malformed input.
2. The ``/csrf`` issue endpoint — returns ``enabled=false`` when
   disabled, requires auth when enabled, ties tokens to the requester.
3. The middleware — enforces only on state-changing methods, exempts
   the issuance endpoint, accepts a fresh token, rejects missing /
   expired / cross-user / forged.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.csrf import (
    CSRFTokenManager,
    CSRFValidationError,
    build_token_manager,
)
from caliber.server import create_app

# ---------------------------------------------------------------------------
# Pure token math
# ---------------------------------------------------------------------------


def _manager(secret: bytes = b"shhh", ttl: int = 3600) -> CSRFTokenManager:
    return CSRFTokenManager(secret=secret, ttl_seconds=ttl)


def test_issue_returns_dot_separated_timestamp_and_signature() -> None:
    manager = _manager()
    token = manager.issue("@alice", now=1700000000)
    timestamp_part, _, signature_part = token.partition(".")
    assert timestamp_part == "1700000000"
    # SHA-256 hex digest is always 64 chars.
    assert len(signature_part) == 64
    assert all(c in "0123456789abcdef" for c in signature_part)


def test_validate_accepts_fresh_token() -> None:
    manager = _manager()
    now = 1700000000
    token = manager.issue("@alice", now=now)
    manager.validate(token, "@alice", now=now + 10)  # no exception


def test_validate_rejects_cross_user_token() -> None:
    """A token issued for ``@alice`` must not validate when presented
    by ``@bob`` — the user is part of the signed payload."""
    manager = _manager()
    now = 1700000000
    token = manager.issue("@alice", now=now)
    with pytest.raises(CSRFValidationError, match=r"signature"):
        manager.validate(token, "@bob", now=now + 10)


def test_validate_rejects_expired_token() -> None:
    manager = _manager(ttl=3600)
    now = 1700000000
    token = manager.issue("@alice", now=now)
    with pytest.raises(CSRFValidationError, match=r"expired"):
        manager.validate(token, "@alice", now=now + 4000)  # 1h+60s skew exceeded


def test_validate_rejects_future_timestamp_beyond_skew() -> None:
    """A token claiming to be from far in the future is treated as
    forged. Small skew (a minute either way) is allowed; bigger gaps
    are not."""
    manager = _manager()
    now = 1700000000
    token = manager.issue("@alice", now=now + 600)  # 10 min ahead
    with pytest.raises(CSRFValidationError, match=r"future"):
        manager.validate(token, "@alice", now=now)


def test_validate_rejects_malformed_token() -> None:
    manager = _manager()
    with pytest.raises(CSRFValidationError, match=r"malformed"):
        manager.validate("not-a-real-token", "@alice")
    with pytest.raises(CSRFValidationError, match=r"malformed"):
        manager.validate("abc.def", "@alice")  # ts not an int


def test_validate_rejects_missing_token() -> None:
    manager = _manager()
    with pytest.raises(CSRFValidationError, match=r"missing"):
        manager.validate(None, "@alice")


def test_validate_rejects_tampered_signature() -> None:
    """Flipping a hex character in the signature must fail validation
    via the constant-time compare (``hmac.compare_digest``)."""
    manager = _manager()
    now = 1700000000
    token = manager.issue("@alice", now=now)
    timestamp_part, _, sig = token.partition(".")
    tampered = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    with pytest.raises(CSRFValidationError, match=r"signature"):
        manager.validate(f"{timestamp_part}.{tampered}", "@alice", now=now)


def test_validate_rejects_tampered_timestamp() -> None:
    """A receiver-side replay attempt with a different timestamp fails
    because the timestamp is part of the signed string."""
    manager = _manager()
    now = 1700000000
    token = manager.issue("@alice", now=now)
    _, _, sig = token.partition(".")
    with pytest.raises(CSRFValidationError, match=r"signature"):
        manager.validate(f"{now + 1}.{sig}", "@alice", now=now)


# ---------------------------------------------------------------------------
# build_token_manager (config glue)
# ---------------------------------------------------------------------------


def test_build_disabled_when_enabled_false() -> None:
    manager = build_token_manager(
        enabled=False,
        secret_env_var="WHATEVER",
        ttl_seconds=3600,
        environ={"WHATEVER": "kk"},
    )
    assert not manager.is_enabled


def test_build_disabled_when_secret_missing() -> None:
    """Operators must set the secret env var for CSRF to actually
    engage — silently producing tokens with an empty secret would be
    worse than an error because tokens would be forgeable."""
    manager = build_token_manager(
        enabled=True,
        secret_env_var="MISSING_SECRET",
        ttl_seconds=3600,
        environ={},
    )
    assert not manager.is_enabled


def test_build_enabled_when_both_present() -> None:
    manager = build_token_manager(
        enabled=True,
        secret_env_var="CSRF_KEY",
        ttl_seconds=600,
        environ={"CSRF_KEY": "secret-bytes"},
    )
    assert manager.is_enabled
    assert manager.ttl_seconds == 600


# ---------------------------------------------------------------------------
# End-to-end via the live app
# ---------------------------------------------------------------------------


def _build_csrf_client(
    *,
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    secret_value: str | None = "csrf-test-secret",
) -> TestClient:
    """Build a TestClient with CSRF wired up via the real config + server.

    Both the config-loader and the runtime ``build_token_manager``
    need to see the signing secret. ``CaliberConfig.load(environ=...)``
    takes the explicit dict, but ``build_token_manager`` reads from
    :func:`os.environ` directly so the secret never lands on the
    config object. ``monkeypatch.setenv`` covers both paths.
    """
    db_path = tmp_path / "caliber-csrf.db"
    monkeypatch.setenv("CALIBER_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("CALIBER_ADMIN_USERS", "@admin")
    monkeypatch.setenv("CALIBER_CSRF_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CALIBER_CSRF_SIGNING_SECRET_ENV", "CSRF_TEST_KEY")
    if secret_value is None:
        monkeypatch.delenv("CSRF_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("CSRF_TEST_KEY", secret_value)

    config = CaliberConfig.load()  # reads from the patched os.environ
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    return TestClient(app, headers={"X-CALIBER-User": "@admin"})


@pytest.fixture
def csrf_enabled_client(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    client = _build_csrf_client(
        tmp_path=tmp_path,
        engine=engine,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
        enabled=True,
    )
    with client:
        yield client


@pytest.fixture
def csrf_disabled_client(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    client = _build_csrf_client(
        tmp_path=tmp_path,
        engine=engine,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
        enabled=False,
    )
    with client:
        yield client


def test_csrf_endpoint_returns_enabled_false_when_disabled(
    csrf_disabled_client: TestClient,
) -> None:
    response = csrf_disabled_client.get("/ajax-api/2.0/mlflow/caliber/csrf")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is False
    assert data["token"] is None


def test_csrf_endpoint_issues_token_when_enabled(
    csrf_enabled_client: TestClient,
) -> None:
    response = csrf_enabled_client.get("/ajax-api/2.0/mlflow/caliber/csrf")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is True
    assert data["token"] is not None
    assert "." in data["token"]
    assert data["ttl_seconds"] == 3600


def test_csrf_endpoint_issues_an_identity_bound_token_to_anonymous_callers(
    csrf_enabled_client: TestClient,
) -> None:
    """Anonymous issuance is required, not a relaxation (N1).

    This test previously asserted 401 for an anonymous caller, on the reasoning that
    such a caller could not perform a write anyway. Session authentication falsified
    that: ``POST /auth/login`` *is* an anonymous state-changing write, so requiring a
    token to obtain a token made login impossible whenever CSRF was enabled.

    The token stays **bound to the caller's identity**, which is what keeps this safe —
    a pre-login token does not authorize a post-login write. That property is asserted
    in ``tests/test_auth_csrf_composition.py`` against the real middleware stack.
    """
    response = csrf_enabled_client.get(
        "/ajax-api/2.0/mlflow/caliber/csrf",
        headers={"X-CALIBER-User": ""},
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["enabled"] is True
    assert body["token"]


def test_writes_without_csrf_token_are_rejected_when_enabled(
    csrf_enabled_client: TestClient,
) -> None:
    """A write without ``X-CALIBER-CSRF`` returns 403."""
    response = csrf_enabled_client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        json={
            "agent_id": "a-1",
            "experiment_id": "exp-1",
            "name": "a",
            "owner": "@x",
        },
    )
    assert response.status_code == 403
    assert "csrf" in response.json()["detail"].lower()


def test_writes_with_fresh_csrf_token_pass_when_enabled(
    csrf_enabled_client: TestClient,
) -> None:
    """The happy path: fetch token, include it on a write, request
    reaches the route handler."""
    issue = csrf_enabled_client.get("/ajax-api/2.0/mlflow/caliber/csrf")
    token = issue.json()["data"]["token"]
    response = csrf_enabled_client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        json={
            "agent_id": "a-1",
            "experiment_id": "exp-1",
            "name": "a",
            "owner": "@x",
        },
        headers={"X-CALIBER-CSRF": token},
    )
    assert response.status_code == 201


def test_writes_with_token_from_different_user_are_rejected(
    csrf_enabled_client: TestClient,
) -> None:
    """User A fetches a token, then user B tries to use it. The
    middleware should reject because the token is signed against
    user A's identity."""
    issue = csrf_enabled_client.get(
        "/ajax-api/2.0/mlflow/caliber/csrf",
        headers={"X-CALIBER-User": "@admin"},
    )
    alice_token = issue.json()["data"]["token"]

    response = csrf_enabled_client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        json={
            "agent_id": "a-2",
            "experiment_id": "exp-2",
            "name": "a",
            "owner": "@x",
        },
        headers={
            "X-CALIBER-User": "@bob",
            "X-CALIBER-CSRF": alice_token,
        },
    )
    assert response.status_code == 403


def test_reads_skip_csrf_check_when_enabled(csrf_enabled_client: TestClient) -> None:
    """``GET`` requests don't carry CSRF tokens; the middleware passes
    them through. Critical for the SPA's initial-page loads."""
    response = csrf_enabled_client.get("/ajax-api/2.0/mlflow/caliber/agents")
    assert response.status_code == 200


def test_csrf_endpoint_itself_is_exempt(csrf_enabled_client: TestClient) -> None:
    """The bootstrap path: the SPA can fetch a token without already
    having one. The endpoint is in the exempt set."""
    response = csrf_enabled_client.get("/ajax-api/2.0/mlflow/caliber/csrf")
    assert response.status_code == 200


def test_expired_token_is_rejected(csrf_enabled_client: TestClient) -> None:
    """Tokens past their TTL get bounced. We construct an obviously-
    expired token by reaching into the manager rather than waiting."""
    # The manager on app.state is the live one; we issue with a
    # backdated timestamp so the request-time validator sees expiry.
    manager: CSRFTokenManager = csrf_enabled_client.app.state.csrf_manager  # type: ignore[attr-defined]
    stale = manager.issue("@admin", now=int(time.time()) - manager.ttl_seconds - 120)
    response = csrf_enabled_client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        json={
            "agent_id": "a-3",
            "experiment_id": "exp-3",
            "name": "a",
            "owner": "@x",
        },
        headers={"X-CALIBER-CSRF": stale},
    )
    assert response.status_code == 403
    assert "expired" in response.json()["detail"].lower()


def test_csrf_disabled_passes_writes_through(csrf_disabled_client: TestClient) -> None:
    """The default deployment shape: the middleware is installed but
    short-circuits because the manager isn't enabled."""
    response = csrf_disabled_client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        json={
            "agent_id": "a-4",
            "experiment_id": "exp-4",
            "name": "a",
            "owner": "@x",
        },
    )
    assert response.status_code == 201
