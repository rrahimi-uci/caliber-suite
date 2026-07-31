"""Regression tests for the identity boundary (C1).

The prior login was not a boundary: the SPA validated ``admin/admin`` in the
browser, sent the resulting identity as ``X-CALIBER-User``, and the backend trusted
it — and with the shipped Compose values a request with *no* header was an admin.

These tests run against the **shipped default** (``auth_mode=session``), unlike the
rest of the suite, which declares ``trusted_header`` because it asserts
authorization. They pin four properties:

1. credentials are verified server-side, and a header cannot substitute for one;
2. sessions are revocable — logout, disable, and password change take effect
   immediately rather than whenever a token happens to expire;
3. failed logins are throttled, so a server-side check is not merely a slower
   oracle; and
4. the login response cannot be used to enumerate accounts.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import Base, CaliberSession, CaliberUserAccount
from caliber.routes.auth import (
    ACCOUNTS_PATH,
    LOGIN_PATH,
    LOGOUT_PATH,
    SESSION_PATH,
    _reset_login_throttle_for_tests,
)
from caliber.server import create_app
from caliber.sessions import (
    AccountError,
    AuthenticationError,
    authenticate,
    create_account,
    hash_password,
    needs_rehash,
    resolve_session,
    revoke_session,
    set_disabled,
    set_password,
    verify_password,
)

GOOD_PASSWORD = "correct-horse-battery"
ADMIN_ID = "@owner"


@pytest.fixture(autouse=True)
def _clear_throttle() -> Iterator[None]:
    _reset_login_throttle_for_tests()
    yield
    _reset_login_throttle_for_tests()


@pytest.fixture
def session_client(tmp_path: object) -> Iterator[TestClient]:
    """An app in the **shipped** session mode, with one seeded admin account."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'auth.db'}",
            # Explicit: the conftest default would otherwise make this
            # trusted_header, and this file's whole subject is session mode.
            "CALIBER_AUTH_MODE": "session",
            "CALIBER_ADMIN_USERS": ADMIN_ID,
            # This fixture creates its own named admin below. Keep the local
            # bootstrap out so inventory assertions remain about that account.
            "CALIBER_AUTH_BOOTSTRAP_ADMIN_USER": "",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            # Plain HTTP in tests: a Secure cookie would be dropped by the client.
            "CALIBER_AUTH_SESSION_COOKIE_SECURE": "false",
        }
    )
    engine = create_engine(config.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = factory
    with factory() as db:
        create_account(db, user_id=ADMIN_ID, password=GOOD_PASSWORD, actor="test")
        db.commit()
    with TestClient(app) as client:
        yield client
    engine.dispose()


@pytest.fixture
def default_admin_client(tmp_path: object) -> Iterator[TestClient]:
    """A fresh zero-credential app bootstraps the requested local superuser."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'default-admin.db'}",
            "CALIBER_AUTH_MODE": "session",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            "CALIBER_AUTH_SESSION_COOKIE_SECURE": "false",
            "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT": "true",
            # The development launchers grant the bootstrap identity its admin
            # scope explicitly.  Keep that authorization opt-in separate from
            # merely creating an account named ``admin``.
            "CALIBER_ADMIN_USERS": "admin",
        }
    )
    engine = create_engine(config.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(config=config)) as client:
        yield client


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_password_hashing_is_salted_and_verifies() -> None:
    first = hash_password(GOOD_PASSWORD)
    second = hash_password(GOOD_PASSWORD)
    assert first != second  # salted, so identical passwords differ at rest
    assert first.startswith("scrypt$")
    assert GOOD_PASSWORD not in first  # never stored recoverably
    assert verify_password(GOOD_PASSWORD, first) is True
    assert verify_password("wrong", first) is False


def test_a_malformed_stored_hash_fails_closed_rather_than_raising() -> None:
    """A corrupted row must fail the login, not 500 the endpoint."""
    for junk in ("", "not-a-hash", "scrypt$bad", "argon2$1$2$3$4$5"):
        assert verify_password(GOOD_PASSWORD, junk) is False


def test_needs_rehash_flags_weaker_parameters() -> None:
    assert needs_rehash(hash_password(GOOD_PASSWORD)) is False
    assert needs_rehash("scrypt$1024$8$1$aa$bb") is True
    assert needs_rehash("garbage") is True


@pytest.mark.parametrize("weak", ["admin", "password", "changeme", "CALIBER", "short"])
def test_well_known_and_short_passwords_are_rejected(db_session: Session, weak: str) -> None:
    """The specific thing this replaces is a four-character well-known default."""
    with pytest.raises(AccountError):
        create_account(db_session, user_id="@x", password=weak)


def test_fresh_app_bootstraps_admin_admin_as_a_hashed_superuser(
    default_admin_client: TestClient,
) -> None:
    login = default_admin_client.post(
        LOGIN_PATH,
        json={"user_id": "admin", "password": "admin"},
    )

    assert login.status_code == 200, login.text
    info = default_admin_client.get(SESSION_PATH).json()["data"]
    assert info["user_id"] == "admin"
    assert info["is_admin"] is True
    with default_admin_client.app.state.session_factory() as session:
        account = session.get(CaliberUserAccount, "admin")
        assert account is not None
        assert account.password_hash != "admin"
        assert "admin" not in account.password_hash
        assert verify_password("admin", account.password_hash) is True


def test_bootstrap_does_not_reset_an_existing_admin_password(
    tmp_path: object,
) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'default-admin-restart.db'}",
            "CALIBER_AUTH_MODE": "session",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            "CALIBER_AUTH_SESSION_COOKIE_SECURE": "false",
            "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT": "true",
        }
    )
    engine = create_engine(config.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        create_account(session, user_id="admin", password=GOOD_PASSWORD, actor="test")
        session.commit()
    engine.dispose()

    with TestClient(create_app(config=config)) as client:
        assert (
            client.post(
                LOGIN_PATH,
                json={"user_id": "admin", "password": "admin"},
            ).status_code
            == 401
        )
        existing_admin = client.post(
            LOGIN_PATH,
            json={"user_id": "admin", "password": GOOD_PASSWORD},
        )
        assert existing_admin.status_code == 200, existing_admin.text


def test_secure_cookie_config_does_not_invent_the_local_default_password(
    tmp_path: object,
) -> None:
    """The implicit known password belongs to plain-HTTP local startup only."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'secure-no-default.db'}",
            "CALIBER_AUTH_MODE": "session",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            "CALIBER_AUTH_SESSION_COOKIE_SECURE": "true",
            # Even an explicit opt-in cannot make a known credential available to
            # a configuration intended to use Secure cookies.
            "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT": "true",
        }
    )
    engine = create_engine(config.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    with TestClient(create_app(config=config)) as client:
        assert (
            client.post(
                LOGIN_PATH,
                json={"user_id": "admin", "password": "admin"},
            ).status_code
            == 401
        )
        with client.app.state.session_factory() as session:
            assert session.get(CaliberUserAccount, "admin") is None


def test_known_password_source_is_rejected_without_the_explicit_local_opt_in(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming a secret source must not silently turn the normal password-policy bypass on."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    monkeypatch.setenv("KNOWN_BOOTSTRAP_PASSWORD", "admin")
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'known-source.db'}",
            "CALIBER_AUTH_MODE": "session",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            "CALIBER_AUTH_SESSION_COOKIE_SECURE": "false",
            "CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV": "KNOWN_BOOTSTRAP_PASSWORD",
        }
    )
    engine = create_engine(config.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    with TestClient(create_app(config=config)) as client:
        assert (
            client.post(LOGIN_PATH, json={"user_id": "admin", "password": "admin"}).status_code
            == 401
        )
        with client.app.state.session_factory() as session:
            assert session.get(CaliberUserAccount, "admin") is None


@pytest.mark.parametrize("bad_id", ["", "  ", "@a,@admin", "with space", "x" * 300])
def test_invalid_user_ids_are_rejected(db_session: Session, bad_id: str) -> None:
    """A comma would corrupt the comma-separated scope lists the id is matched
    against — an id like ``@a,@admin`` must never reach them."""
    with pytest.raises(AccountError):
        create_account(db_session, user_id=bad_id, password=GOOD_PASSWORD)


def test_duplicate_accounts_are_rejected(db_session: Session) -> None:
    create_account(db_session, user_id="@dup", password=GOOD_PASSWORD)
    with pytest.raises(AccountError, match="already exists"):
        create_account(db_session, user_id="@dup", password=GOOD_PASSWORD)


# ---------------------------------------------------------------------------
# Session lifecycle at the unit level
# ---------------------------------------------------------------------------


def test_authenticate_issues_a_resolvable_session(db_session: Session) -> None:
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=3600)
    assert resolve_session(db_session, issued.token) == "@u"


def test_only_a_token_fingerprint_is_stored(db_session: Session) -> None:
    """Reading the sessions table must not yield a usable credential."""
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=3600)
    row = db_session.query(CaliberSession).one()
    assert row.token_hash != issued.token
    assert issued.token not in row.token_hash


def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(
    db_session: Session,
) -> None:
    """Distinguishing them turns login into a user-enumeration oracle."""
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    with pytest.raises(AuthenticationError) as wrong:
        authenticate(db_session, user_id="@u", password="not-the-password", ttl_seconds=60)
    with pytest.raises(AuthenticationError) as unknown:
        authenticate(db_session, user_id="@nobody", password=GOOD_PASSWORD, ttl_seconds=60)
    assert str(wrong.value) == str(unknown.value)


def test_unknown_user_does_not_build_a_second_scrypt_hash_per_attempt(
    db_session: Session,
) -> None:
    """The dummy verifier must cost one hash operation, like a wrong real password.

    Building the dummy hash inside ``authenticate`` costs one scrypt and verifying it
    costs another, making unknown accounts measurably slower and restoring enumeration.
    """
    with (
        patch("caliber.sessions.verify_password", return_value=False) as verify,
        patch("caliber.sessions.hash_password") as build_hash,
        pytest.raises(AuthenticationError),
    ):
        authenticate(db_session, user_id="@nobody", password=GOOD_PASSWORD, ttl_seconds=60)
    verify.assert_called_once()
    build_hash.assert_not_called()


def test_a_revoked_session_stops_resolving(db_session: Session) -> None:
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=3600)
    assert revoke_session(db_session, issued.token) is True
    assert resolve_session(db_session, issued.token) is None
    # Revoking twice is not an error, but reports that nothing changed.
    assert revoke_session(db_session, issued.token) is False


def test_an_expired_session_stops_resolving(db_session: Session) -> None:
    from datetime import datetime, timedelta, timezone

    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=60)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert resolve_session(db_session, issued.token, now=later) is None


def test_disabling_an_account_invalidates_its_live_session(db_session: Session) -> None:
    """Enforced at *resolution*, not only at login — otherwise disabling an account
    would take effect whenever its session happened to expire."""
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=3600)
    set_disabled(db_session, user_id="@u", disabled=True)
    assert resolve_session(db_session, issued.token) is None
    with pytest.raises(AuthenticationError):
        authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=60)


def test_changing_a_password_revokes_existing_sessions(db_session: Session) -> None:
    """A password change that leaves old sessions valid does not lock anyone out,
    which is the reason people change passwords."""
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=3600)
    set_password(db_session, user_id="@u", password="a-completely-new-secret")
    assert resolve_session(db_session, issued.token) is None
    assert verify_password(
        "a-completely-new-secret", db_session.get(CaliberUserAccount, "@u").password_hash
    )


def test_a_deleted_account_invalidates_its_session(db_session: Session) -> None:
    create_account(db_session, user_id="@u", password=GOOD_PASSWORD)
    issued = authenticate(db_session, user_id="@u", password=GOOD_PASSWORD, ttl_seconds=3600)
    db_session.delete(db_session.get(CaliberUserAccount, "@u"))
    db_session.flush()
    assert resolve_session(db_session, issued.token) is None


def test_a_garbage_token_resolves_to_nothing(db_session: Session) -> None:
    assert resolve_session(db_session, "") is None
    assert resolve_session(db_session, "not-a-real-token") is None


# ---------------------------------------------------------------------------
# The HTTP boundary, in the shipped session mode
# ---------------------------------------------------------------------------


def test_a_self_asserted_header_is_not_an_identity(session_client: TestClient) -> None:
    """The core of C1. Under the shipped default, asserting an identity does
    nothing — including asserting the seeded admin's."""
    response = session_client.get(ACCOUNTS_PATH, headers={"X-CALIBER-User": ADMIN_ID})
    assert response.status_code == 401

    info = session_client.get(SESSION_PATH, headers={"X-CALIBER-User": ADMIN_ID}).json()["data"]
    assert info["user_id"] == "anonymous"
    assert info["auth_mode"] == "session"
    assert info["authenticated_by"] == "none"
    assert info["login_required"] is True


def test_login_then_authenticated_request_then_logout(session_client: TestClient) -> None:
    login = session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID, "password": GOOD_PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["data"]["user_id"] == ADMIN_ID
    assert "token" not in login.json()["data"], "the HttpOnly bearer must not be echoed to JS"

    # The cookie is HttpOnly, so a browser would not expose it to script.
    set_cookie = login.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()

    # The session — not any header — is what authorizes the request.
    listed = session_client.get(ACCOUNTS_PATH)
    assert listed.status_code == 200, listed.text
    assert [a["user_id"] for a in listed.json()["data"]["accounts"]] == [ADMIN_ID]

    info = session_client.get(SESSION_PATH).json()["data"]
    assert info["user_id"] == ADMIN_ID
    assert info["authenticated_by"] == "session"
    assert info["is_admin"] is True
    assert info["login_required"] is False

    assert session_client.post(LOGOUT_PATH).json()["data"]["revoked"] is True
    # Server-side revocation, so the token is dead even if it were replayed.
    assert session_client.get(ACCOUNTS_PATH).status_code == 401


def test_a_bearer_token_works_for_non_browser_clients(session_client: TestClient) -> None:
    # The browser login endpoint no longer echoes the HttpOnly credential in JSON.
    # A separately provisioned bearer still works for API clients.
    with session_client.app.state.session_factory() as session:
        token = authenticate(
            session,
            user_id=ADMIN_ID,
            password=GOOD_PASSWORD,
            ttl_seconds=3600,  # gitleaks:allow
        ).token
        session.commit()
    session_client.cookies.clear()
    response = session_client.get(ACCOUNTS_PATH, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_a_wrong_password_is_401_and_leaks_nothing(session_client: TestClient) -> None:
    wrong = session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID, "password": "nope"})
    unknown = session_client.post(LOGIN_PATH, json={"user_id": "@ghost", "password": GOOD_PASSWORD})
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_repeated_failures_are_throttled(session_client: TestClient) -> None:
    """A server-side password check that can be retried without limit is still an
    oracle, just a slower one."""
    for _ in range(5):
        assert (
            session_client.post(
                LOGIN_PATH, json={"user_id": ADMIN_ID, "password": "wrong"}
            ).status_code
            == 401
        )
    throttled = session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID, "password": "wrong"})
    assert throttled.status_code == 429
    # Even the *correct* password is refused while throttled, so the throttle
    # cannot be sidestepped by guessing right on the sixth attempt.
    assert (
        session_client.post(
            LOGIN_PATH, json={"user_id": ADMIN_ID, "password": GOOD_PASSWORD}
        ).status_code
        == 429
    )


def test_username_spray_hits_the_client_wide_login_budget(session_client: TestClient) -> None:
    """A new user id per request must not grant a fresh five-attempt budget forever."""
    with patch("caliber.routes.auth.authenticate", side_effect=AuthenticationError):
        for index in range(20):
            response = session_client.post(
                LOGIN_PATH,
                json={"user_id": f"@spray-{index}", "password": "wrong"},
            )
            assert response.status_code == 401, response.text
        blocked = session_client.post(
            LOGIN_PATH,
            json={"user_id": "@spray-next", "password": "wrong"},
        )
    assert blocked.status_code == 429


def test_login_requires_both_fields(session_client: TestClient) -> None:
    assert session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID}).status_code == 400
    assert session_client.post(LOGIN_PATH, json={"password": GOOD_PASSWORD}).status_code == 400


def test_account_administration_requires_an_authenticated_admin(
    session_client: TestClient,
) -> None:
    assert (
        session_client.post(
            ACCOUNTS_PATH, json={"user_id": "@new", "password": GOOD_PASSWORD}
        ).status_code
        == 401
    )

    session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID, "password": GOOD_PASSWORD})
    created = session_client.post(
        ACCOUNTS_PATH, json={"user_id": "@new", "password": "another-strong-secret"}
    )
    assert created.status_code == 201, created.text
    # A weak password is refused even by an admin.
    weak = session_client.post(ACCOUNTS_PATH, json={"user_id": "@weak", "password": "admin"})
    assert weak.status_code == 400
    # The one-time bootstrap exception must not leak into the normal reset path.
    weak_reset = session_client.patch(f"{ACCOUNTS_PATH}/@new", json={"password": "admin"})
    assert weak_reset.status_code == 400


def test_disabling_an_account_over_http_kills_its_session(session_client: TestClient) -> None:
    session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID, "password": GOOD_PASSWORD})
    session_client.post(
        ACCOUNTS_PATH, json={"user_id": "@victim", "password": "victim-strong-secret"}
    )
    victim = TestClient(session_client.app)
    victim.post(LOGIN_PATH, json={"user_id": "@victim", "password": "victim-strong-secret"})
    assert victim.get(SESSION_PATH).json()["data"]["user_id"] == "@victim"

    disabled = session_client.patch(f"{ACCOUNTS_PATH}/@victim", json={"disabled": True})
    assert disabled.status_code == 200, disabled.text
    # Immediately, not at expiry.
    assert victim.get(SESSION_PATH).json()["data"]["user_id"] == "anonymous"


def test_account_administration_never_returns_a_password_hash(
    session_client: TestClient,
) -> None:
    session_client.post(LOGIN_PATH, json={"user_id": ADMIN_ID, "password": GOOD_PASSWORD})
    body = session_client.get(ACCOUNTS_PATH).text
    assert "scrypt$" not in body
    assert GOOD_PASSWORD not in body
