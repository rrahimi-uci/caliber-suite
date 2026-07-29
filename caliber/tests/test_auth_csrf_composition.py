"""N1: session authentication and CSRF protection must work **together**.

An independent review found the two controls deadlocked whenever both were enabled —
which is the production posture — so the shipped default made signing in impossible:

    POST /auth/login -> 403 "CSRF check failed: missing CSRF token"
    GET  /csrf       -> 401 "authentication required"

Two separate causes, both fixed, both regression-tested here:

1. **``/csrf`` required authentication.** Its docstring justified that with "an
   anonymous caller ... wouldn't be able to use one for a write anyway, since RBAC
   rejects anonymous writes with 401". True while identity came from a header — every
   caller was already identified. Session auth falsified it: login *is* an anonymous
   state-changing write.
2. **The middleware resolved identity differently from the routes.** It read
   ``X-CALIBER-User`` directly while the routes resolved a session, so ``/csrf`` bound
   a token to the signed-in user and the middleware validated it against ``anonymous``.
   Every authenticated write then failed with "invalid CSRF token signature".

Why this file exists at all, given both controls had passing tests: each was tested in
isolation, and in isolation each was correct. The defect lived only in their
*composition*, which is why the suite was green while the product was unusable. These
tests drive the real app with both features on.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import Base
from caliber.db.session import create_engine_from_config
from caliber.server import create_app

PREFIX = "/ajax-api/2.0/mlflow/caliber"
PASSWORD = "correct-horse-battery-9"


@pytest.fixture
def csrf_client(tmp_path, monkeypatch) -> TestClient:
    """A real app with **both** session auth and CSRF enabled.

    Deliberately not the shared ``client`` fixture: that one runs in
    ``trusted_header`` mode with CSRF off, which is precisely the configuration in
    which this defect is invisible.
    """
    monkeypatch.setenv("CSRF_SECRET", "s" * 32)
    monkeypatch.setenv("BOOT_PW", PASSWORD)
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path}/csrf.db",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            # Set explicitly rather than relied on as a default: the shared conftest
            # exports CALIBER_AUTH_MODE=trusted_header for the rest of the suite, and
            # inheriting it here would silently test the wrong mode — the one where this
            # defect does not appear.
            "CALIBER_AUTH_MODE": "session",
            "CALIBER_CSRF_ENABLED": "true",
            "CALIBER_CSRF_SIGNING_SECRET_ENV": "CSRF_SECRET",
            "CALIBER_AUTH_BOOTSTRAP_ADMIN_USER": "@owner",
            "CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV": "BOOT_PW",
            # Plain-HTTP test client would drop a Secure cookie.
            "CALIBER_AUTH_SESSION_COOKIE_SECURE": "false",
        }
    )
    # Disposed explicitly: an undisposed engine leaves an open SQLite handle that
    # Python 3.14 surfaces as a ResourceWarning, which pytest promotes to an error.
    engine = create_engine_from_config(config)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(config)) as client:
        yield client


def _token(client: TestClient) -> str:
    response = client.get(f"{PREFIX}/csrf")
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def test_an_anonymous_client_can_obtain_a_csrf_token(csrf_client: TestClient) -> None:
    """The bootstrap. Without this there is no way to make the first write, and the
    first write a session-mode client must make is signing in."""
    body = csrf_client.get(f"{PREFIX}/csrf").json()["data"]
    assert body["enabled"] is True
    assert body["token"]


def test_login_succeeds_with_an_anonymous_token_and_the_session_works(
    csrf_client: TestClient,
) -> None:
    """The exact flow that was impossible: token -> login -> authenticated request."""
    login = csrf_client.post(
        f"{PREFIX}/auth/login",
        json={"user_id": "@owner", "password": PASSWORD},
        headers={"X-CALIBER-CSRF": _token(csrf_client)},
    )
    assert login.status_code == 200, login.text
    assert login.json()["data"]["user_id"] == "@owner"

    session = csrf_client.get(f"{PREFIX}/auth/session")
    assert session.status_code == 200, session.text
    assert session.json()["data"]["user_id"] == "@owner"
    assert session.json()["data"]["auth_mode"] == "session"


def test_login_is_still_csrf_protected(csrf_client: TestClient) -> None:
    """The fix must not become a hole. Issuing anonymous tokens was chosen over
    exempting ``/auth/login`` precisely so login keeps its CSRF protection — a
    forced-login CSRF is a real attack, even if a lesser one."""
    login = csrf_client.post(
        f"{PREFIX}/auth/login", json={"user_id": "@owner", "password": PASSWORD}
    )
    assert login.status_code == 403
    assert "CSRF" in login.json()["detail"]


def test_an_authenticated_write_validates_against_the_session_identity(
    csrf_client: TestClient,
) -> None:
    """The middleware half of N1.

    After login the client's identity comes from the session cookie, not a header. A
    token issued to that identity must validate — it did not, because the middleware
    still read ``X-CALIBER-User`` and saw ``anonymous``.
    """
    csrf_client.post(
        f"{PREFIX}/auth/login",
        json={"user_id": "@owner", "password": PASSWORD},
        headers={"X-CALIBER-CSRF": _token(csrf_client)},
    )
    # A fresh token, now bound to @owner via the session.
    logout = csrf_client.post(
        f"{PREFIX}/auth/logout", headers={"X-CALIBER-CSRF": _token(csrf_client)}
    )
    assert logout.status_code == 200, logout.text
    assert logout.json()["data"]["revoked"] is True


def test_a_pre_login_token_does_not_authorize_a_post_login_write(
    csrf_client: TestClient,
) -> None:
    """Tokens stay identity-bound, which is what makes anonymous issuance safe.

    If a pre-login token kept working after signing in, the anonymous bootstrap would
    have widened CSRF rather than preserved it.
    """
    anonymous_token = _token(csrf_client)
    csrf_client.post(
        f"{PREFIX}/auth/login",
        json={"user_id": "@owner", "password": PASSWORD},
        headers={"X-CALIBER-CSRF": anonymous_token},
    )

    reused = csrf_client.post(f"{PREFIX}/auth/logout", headers={"X-CALIBER-CSRF": anonymous_token})

    assert reused.status_code == 403, reused.text
    assert "CSRF" in reused.json()["detail"]


def test_a_wrong_password_is_still_refused_generically(csrf_client: TestClient) -> None:
    """CSRF changes must not have loosened credential checking."""
    response = csrf_client.post(
        f"{PREFIX}/auth/login",
        json={"user_id": "@owner", "password": "not-the-password"},
        headers={"X-CALIBER-CSRF": _token(csrf_client)},
    )
    assert response.status_code == 401
