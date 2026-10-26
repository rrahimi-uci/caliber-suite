"""The reference deployment profile refuses to start on an unsafe configuration (EGI B12 part 1).

``CALIBER_PROFILE=reference`` makes ``create_app`` raise ``ConfigError`` listing every setting that
breaks the profile, each with the environment variable to change. The default ``development`` profile
must keep starting with the very same settings, so a local install is unaffected.

A *safe* reference configuration needs PostgreSQL, which the default test jobs do not have. The
refusal tests do not need a database at all (the check runs before one is opened), and the one test
that starts a safe reference app runs against the PostgreSQL service of the migration-parity CI job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from starlette.testclient import TestClient

from caliber.config import CaliberConfig, ConfigError
from caliber.db.migrate import build_config
from caliber.profile import ProfileViolation, reference_profile_violations
from caliber.server import create_app
from tests.dialect_helpers import POSTGRES_TEST_URL, dialect_database_url

HEALTH = "/ajax-api/2.0/mlflow/caliber/health"

#: Settings that make a configuration safe for the reference profile. The database URL is never
#: connected to by the refusal tests: the profile check runs first.
SAFE: dict[str, Any] = {
    "profile": "reference",
    "database_url": "postgresql+psycopg://caliber:secret@db.example.invalid:5432/caliber",
    "csrf_enabled": True,
    "auth_dev_fallback_enabled": False,
    "auth_bootstrap_allow_insecure_default": False,
    "auth_mode": "session",
    "auth_trusted_proxy_secret_env": "",
    "egress_policy_enabled": True,
    "mcp_allow_insecure_http": False,
    "approval_allow_self_approval": False,
}

#: code -> (settings that make it the *only* violation, the env var its message must name)
UNSAFE: dict[str, tuple[dict[str, Any], str]] = {
    "sqlite_database": ({"database_url": "sqlite:///./caliber.db"}, "CALIBER_DATABASE_URL"),
    "csrf_disabled": ({"csrf_enabled": False}, "CALIBER_CSRF_ENABLED"),
    "dev_fallback_enabled": (
        {"auth_dev_fallback_enabled": True},
        "CALIBER_AUTH_DEV_FALLBACK_ENABLED",
    ),
    "insecure_bootstrap": (
        {"auth_bootstrap_allow_insecure_default": True},
        "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT",
    ),
    "trusted_header_without_proxy_secret": (
        {"auth_mode": "trusted_header", "auth_trusted_proxy_secret_env": ""},
        "CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV",
    ),
    "egress_policy_disabled": (
        {"egress_policy_enabled": False},
        "CALIBER_EGRESS_POLICY_ENABLED",
    ),
    "mcp_insecure_http": ({"mcp_allow_insecure_http": True}, "CALIBER_MCP_ALLOW_INSECURE_HTTP"),
    "self_approval_allowed": (
        {"approval_allow_self_approval": True},
        "CALIBER_APPROVAL_ALLOW_SELF_APPROVAL",
    ),
}


def _config(base: CaliberConfig, **updates: Any) -> CaliberConfig:
    return base.model_copy(update={**SAFE, **updates})


def _codes(violations: list[ProfileViolation]) -> list[str]:
    return [v.code for v in violations]


# ---------------------------------------------------------------- the config field


def test_profile_defaults_to_development() -> None:
    assert CaliberConfig.load(environ={}).profile == "development"


def test_profile_is_read_from_the_environment() -> None:
    assert CaliberConfig.load(environ={"CALIBER_PROFILE": "reference"}).profile == "reference"


@pytest.mark.parametrize("value", ["bogus", "Reference", "production", ""])
def test_profile_field_validates(value: str) -> None:
    with pytest.raises(ConfigError, match="profile"):
        CaliberConfig.load(environ={"CALIBER_PROFILE": value})


# ---------------------------------------------------------------- the violation list


def test_safe_config_has_no_violations(app_config: CaliberConfig) -> None:
    assert reference_profile_violations(_config(app_config)) == []


def test_eight_violations_in_order(app_config: CaliberConfig) -> None:
    every_unsafe: dict[str, Any] = {}
    for settings, _ in UNSAFE.values():
        every_unsafe.update(settings)
    found = reference_profile_violations(_config(app_config, **every_unsafe))
    assert _codes(found) == list(UNSAFE)


@pytest.mark.parametrize("code", list(UNSAFE))
def test_each_unsafe_setting_alone_is_one_violation(app_config: CaliberConfig, code: str) -> None:
    settings, env_var = UNSAFE[code]
    (violation,) = reference_profile_violations(_config(app_config, **settings))
    assert violation.code == code
    assert violation.env_var == env_var
    assert env_var in violation.message
    assert "for the reference profile" in violation.message


def test_a_trusted_header_config_with_a_proxy_secret_is_allowed(app_config: CaliberConfig) -> None:
    config = _config(
        app_config, auth_mode="trusted_header", auth_trusted_proxy_secret_env="PROXY_SECRET"
    )
    assert reference_profile_violations(config) == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_proxy_secret_name_counts_as_none(app_config: CaliberConfig, blank: str) -> None:
    config = _config(app_config, auth_mode="trusted_header", auth_trusted_proxy_secret_env=blank)
    assert _codes(reference_profile_violations(config)) == ["trusted_header_without_proxy_secret"]


@pytest.mark.parametrize("url", ["SQLITE:///x.db", "sqlite+pysqlite:///x.db", " sqlite:///x.db"])
def test_any_sqlite_url_is_caught(app_config: CaliberConfig, url: str) -> None:
    assert _codes(reference_profile_violations(_config(app_config, database_url=url))) == [
        "sqlite_database"
    ]


def test_a_violation_never_quotes_the_database_url(app_config: CaliberConfig) -> None:
    url = "sqlite:///./hunter2-looking-path.db"
    (violation,) = reference_profile_violations(_config(app_config, database_url=url))
    assert "hunter2" not in violation.message


# ---------------------------------------------------------------- startup


@pytest.mark.parametrize("code", list(UNSAFE))
def test_each_unsafe_setting_refuses_reference_startup(
    app_config: CaliberConfig, code: str
) -> None:
    settings, env_var = UNSAFE[code]
    with pytest.raises(ConfigError) as refused:
        create_app(_config(app_config, **settings))
    assert "reference profile refused" in str(refused.value)
    assert env_var in str(refused.value)


def test_every_violation_is_listed_in_one_refusal(app_config: CaliberConfig) -> None:
    every_unsafe: dict[str, Any] = {}
    for settings, _ in UNSAFE.values():
        every_unsafe.update(settings)
    with pytest.raises(ConfigError) as refused:
        create_app(_config(app_config, **every_unsafe))
    for _, env_var in UNSAFE.values():
        assert env_var in str(refused.value)


def test_a_refused_start_leaves_no_database_behind(
    app_config: CaliberConfig, tmp_path: Path
) -> None:
    """The check runs before the engine is created or the bootstrap admin is written."""
    database = tmp_path / "must-not-exist.db"
    with pytest.raises(ConfigError):
        create_app(_config(app_config, database_url=f"sqlite:///{database}"))
    assert not database.exists()


def test_development_profile_unchanged(app_config: CaliberConfig) -> None:
    every_unsafe: dict[str, Any] = {}
    for settings, _ in UNSAFE.values():
        every_unsafe.update(settings)
    unsafe = app_config.model_copy(
        update={**every_unsafe, "profile": "development", "database_url": app_config.database_url}
    )
    app = create_app(unsafe)
    assert TestClient(app).get(HEALTH).status_code == 200


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="CALIBER_TEST_POSTGRES_URL is not set")
def test_safe_reference_config_starts_on_postgresql(
    app_config: CaliberConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CSRF protection also needs its signing secret: the server refuses to boot with the flag on and
    # no secret, which is right and is part of what "safe" means here.
    monkeypatch.setenv(app_config.csrf_signing_secret_env, "x" * 48)
    with dialect_database_url("postgresql", tmp_path, "profile") as url:
        cfg = build_config()
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        app = create_app(_config(app_config, database_url=url))
        response = TestClient(app).get(HEALTH)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- what the operator reads

#: code -> (setting, the whole message). The messages are the operator's only guide, so they are pinned.
MESSAGES: dict[str, tuple[str, str]] = {
    "sqlite_database": (
        "database_url",
        "database_url is a SQLite file; set CALIBER_DATABASE_URL=<a PostgreSQL URL> for the reference profile",
    ),
    "csrf_disabled": (
        "csrf_enabled",
        "csrf_enabled is false; set CALIBER_CSRF_ENABLED=true for the reference profile",
    ),
    "dev_fallback_enabled": (
        "auth_dev_fallback_enabled",
        "auth_dev_fallback_enabled is true; set CALIBER_AUTH_DEV_FALLBACK_ENABLED=false for the reference profile",
    ),
    "insecure_bootstrap": (
        "auth_bootstrap_allow_insecure_default",
        "auth_bootstrap_allow_insecure_default is true; "
        "set CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=false for the reference profile",
    ),
    "egress_policy_disabled": (
        "egress_policy_enabled",
        "egress_policy_enabled is false; set CALIBER_EGRESS_POLICY_ENABLED=true for the reference profile",
    ),
    "mcp_insecure_http": (
        "mcp_allow_insecure_http",
        "mcp_allow_insecure_http is true; set CALIBER_MCP_ALLOW_INSECURE_HTTP=false for the reference profile",
    ),
    "self_approval_allowed": (
        "approval_allow_self_approval",
        "approval_allow_self_approval is true; "
        "set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=false for the reference profile",
    ),
}


@pytest.mark.parametrize("code", list(MESSAGES))
def test_the_message_and_setting_of_each_violation(app_config: CaliberConfig, code: str) -> None:
    settings, _ = UNSAFE[code]
    (violation,) = reference_profile_violations(_config(app_config, **settings))
    setting, message = MESSAGES[code]
    assert violation.setting == setting
    assert violation.message == message


def test_the_trusted_header_violation_names_both_ways_out(app_config: CaliberConfig) -> None:
    settings, _ = UNSAFE["trusted_header_without_proxy_secret"]
    (violation,) = reference_profile_violations(_config(app_config, **settings))
    assert violation.setting == "auth_mode"
    assert violation.message.startswith("auth_mode is trusted_header with no proxy secret")
    assert "CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV=" in violation.message
    assert "CALIBER_AUTH_MODE=session" in violation.message
    assert violation.message.endswith("for the reference profile")


def test_the_refusal_counts_and_bullets_every_violation(app_config: CaliberConfig) -> None:
    every_unsafe: dict[str, Any] = {}
    for settings, _ in UNSAFE.values():
        every_unsafe.update(settings)
    with pytest.raises(ConfigError) as refused:
        create_app(_config(app_config, **every_unsafe))
    lines = str(refused.value).splitlines()
    assert lines[0] == "reference profile refused (8 violations):"
    assert len(lines) == 1 + len(UNSAFE)
    assert all(line.startswith("- ") for line in lines[1:])


def test_a_single_violation_is_not_pluralised(app_config: CaliberConfig) -> None:
    with pytest.raises(ConfigError) as refused:
        create_app(_config(app_config, csrf_enabled=False))
    assert str(refused.value).splitlines()[0] == "reference profile refused (1 violation):"


# ---------------------------------------------------------------- a refusal precedes everything


def test_a_refusal_happens_before_logging_tracing_the_engine_or_the_bootstrap_admin(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of these has a side effect (S3 log sink, MLflow tracer, a database, an account)."""
    from caliber import server

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("ran before the reference profile was checked")

    for name in (
        "configure_logging",
        "configure_redactor",
        "configure_tracing",
        "create_engine_from_config",
        "_bootstrap_admin_account",
    ):
        monkeypatch.setattr(server, name, must_not_run)
    with pytest.raises(ConfigError, match="reference profile refused"):
        create_app(_config(app_config, csrf_enabled=False))


def test_safe_reference_config_starts(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A safe reference config is accepted by the check and boots. Run everywhere: the PostgreSQL URL is
    only a setting here, the engine behind the app is the test SQLite one (the real PostgreSQL boot is
    ``test_safe_reference_config_starts_on_postgresql``)."""
    from caliber.db.session import create_engine_from_config

    monkeypatch.setattr(
        "caliber.server.create_engine_from_config",
        lambda _config: create_engine_from_config(app_config),
    )
    monkeypatch.setenv(app_config.csrf_signing_secret_env, "x" * 48)
    app = create_app(_config(app_config))
    assert TestClient(app).get(HEALTH).status_code == 200
