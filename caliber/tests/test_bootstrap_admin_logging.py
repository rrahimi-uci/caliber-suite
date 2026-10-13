"""Startup says what is wrong when a first install cannot be logged into (alpha gap P0-5).

A default install started with session auth, no account and no password source, and
logged nothing. A configured bootstrap account with all three role lists empty signed in
as a viewer and could not create a project. Both are now loud.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber import server
from caliber.config import CaliberConfig
from caliber.sessions import create_account


def _config(**environ: str) -> CaliberConfig:
    base = {
        "CALIBER_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "CALIBER_AUTH_MODE": "session",
        "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
    }
    return CaliberConfig.load(environ={**base, **environ})


class _Log:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.infos: list[str] = []
        monkeypatch.setattr(
            server.logger,
            "warning",
            lambda msg, *a, **k: self.warnings.append(msg % a if a else msg),
        )
        monkeypatch.setattr(
            server.logger, "error", lambda msg, *a, **k: self.errors.append(msg % a if a else msg)
        )
        monkeypatch.setattr(
            server.logger, "info", lambda msg, *a, **k: self.infos.append(msg % a if a else msg)
        )


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    return _Log(monkeypatch)


def test_warns_when_session_auth_has_no_account_and_no_password_source(
    session_factory: sessionmaker[Session], log: _Log
) -> None:
    config = _config(CALIBER_AUTH_BOOTSTRAP_ADMIN_USER="admin")

    server._bootstrap_admin_account(config, session_factory)

    assert any(
        "nobody can log in" in w and "CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV" in w
        for w in log.warnings
    ), log.warnings


def test_no_nobody_can_log_in_warning_once_an_account_exists(
    session_factory: sessionmaker[Session], db_session: Session, log: _Log
) -> None:
    create_account(db_session, user_id="someone", password="a-long-enough-password-1!", actor="t")
    db_session.commit()

    server._bootstrap_admin_account(
        _config(CALIBER_AUTH_BOOTSTRAP_ADMIN_USER="admin"), session_factory
    )

    assert not any("nobody can log in" in w for w in log.warnings)


def test_errors_when_the_bootstrap_account_is_created_with_no_role_lists(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, log: _Log
) -> None:
    monkeypatch.setenv("ALPHA_ADMIN_PW", "a-long-enough-password-1!")
    config = _config(
        CALIBER_AUTH_BOOTSTRAP_ADMIN_USER="ops",
        CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV="ALPHA_ADMIN_PW",
    )

    server._bootstrap_admin_account(config, session_factory)

    assert any(
        "CALIBER_ADMIN_USERS" in e and "CALIBER_OPERATOR_USERS" in e and "'ops'" in e
        for e in log.errors
    ), log.errors


def test_no_role_error_when_any_role_list_names_someone(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, log: _Log
) -> None:
    monkeypatch.setenv("ALPHA_ADMIN_PW", "a-long-enough-password-1!")
    config = _config(
        CALIBER_AUTH_BOOTSTRAP_ADMIN_USER="ops",
        CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV="ALPHA_ADMIN_PW",
        CALIBER_ADMIN_USERS="ops",
    )

    server._bootstrap_admin_account(config, session_factory)

    assert log.errors == []
