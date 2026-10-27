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
    "memory_inference_enabled": (
        {"memory_enabled": True, "memory_infer": True},
        "CALIBER_MEMORY_INFER",
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
        client = TestClient(app)
        response = client.get(HEALTH)
        # The reference profile also bounds request bodies: one byte over the limit is refused outright.
        oversized = client.post(
            HEALTH, content=b"x" * (app.state.config.reference_max_body_bytes + 1)
        )
    assert response.status_code == 200, response.text
    assert oversized.status_code == 413, oversized.text
    assert oversized.json()["reason"] == "request_too_large"


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
    assert lines[0] == "reference profile refused (9 violations):"
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


# ---------------------------------------------------------------- armed fault barriers


def test_an_armed_fault_barrier_breaks_the_profile(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_FAULT_BARRIERS", "before_dispatch, after_outbox_commit")
    (violation,) = reference_profile_violations(_config(app_config))
    assert violation.code == "fault_barriers_enabled"
    assert violation.setting == "fault_barriers"
    assert violation.env_var == "CALIBER_FAULT_BARRIERS"
    assert violation.message == (
        "fault barriers are armed (after_outbox_commit, before_dispatch), so this process exits on "
        "purpose when it reaches one; unset CALIBER_FAULT_BARRIERS for the reference profile"
    )


@pytest.mark.parametrize("value", [None, "", "  ", " , "])
def test_unset_or_blank_fault_barriers_are_not_a_violation(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("CALIBER_FAULT_BARRIERS", raising=False)
    else:
        monkeypatch.setenv("CALIBER_FAULT_BARRIERS", value)
    assert reference_profile_violations(_config(app_config)) == []


def test_the_reference_profile_refuses_to_start_with_an_armed_barrier(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBER_FAULT_BARRIERS", "after_downstream_commit")
    with pytest.raises(ConfigError) as refused:
        create_app(_config(app_config))
    assert str(refused.value).splitlines()[0] == "reference profile refused (1 violation):"
    assert "after_downstream_commit" in str(refused.value)


def test_an_armed_barrier_is_logged_in_every_profile(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CALIBER_FAULT_BARRIERS", "after_outbox_commit,before_dispatch")
    with caplog.at_level("WARNING", logger="caliber"):
        create_app(app_config)
    (record,) = [r for r in caplog.records if "fault barriers are armed" in r.getMessage()]
    assert "after_outbox_commit, before_dispatch" in record.getMessage()
    assert "CALIBER_FAULT_BARRIERS" in record.getMessage()


def test_no_warning_without_an_armed_barrier(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("CALIBER_FAULT_BARRIERS", raising=False)
    with caplog.at_level("WARNING", logger="caliber"):
        create_app(app_config)
    assert not [r for r in caplog.records if "fault barriers" in r.getMessage()]


def test_the_development_profile_starts_with_an_armed_barrier(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenarios arm barriers on development-profile apps; only the reference profile forbids it."""
    monkeypatch.setenv("CALIBER_FAULT_BARRIERS", "before_dispatch")
    assert TestClient(create_app(app_config)).get(HEALTH).status_code == 200


# ---------------------------------------------------------------- the operator page

PAGE = Path(__file__).resolve().parents[2] / "docs" / "operate" / "reference-profile.md"
needs_docs = pytest.mark.skipif(not PAGE.exists(), reason="repository docs not present")


def _page_requirements() -> list[list[str]]:
    section = PAGE.read_text(encoding="utf-8").split("## What it requires", 1)[1]
    lines = []
    for line in section.splitlines()[1:]:
        if line.startswith("|"):
            lines.append(line)
        elif lines:
            break
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in lines[2:]]


@needs_docs
def test_operator_page_lists_every_requirement() -> None:
    from caliber.profile import REFERENCE_REQUIREMENTS

    rows = _page_requirements()
    assert [row[0].strip("`") for row in rows] == [r.code for r in REFERENCE_REQUIREMENTS]
    for row, requirement in zip(rows, REFERENCE_REQUIREMENTS, strict=True):
        assert row[1].strip("`") == requirement.setting
        assert row[2].strip("`") == requirement.env_var
        assert row[3] == requirement.required
        assert row[4] == requirement.why


def test_the_requirements_table_matches_the_rules(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every rule has a requirement and every requirement is a rule, with the same code and variable."""
    from caliber.profile import REFERENCE_REQUIREMENTS

    every_unsafe: dict[str, Any] = {}
    for settings, _ in UNSAFE.values():
        every_unsafe.update(settings)
    monkeypatch.setenv("CALIBER_FAULT_BARRIERS", "before_dispatch")
    found = reference_profile_violations(_config(app_config, **every_unsafe))
    assert [(v.code, v.env_var) for v in found] == [
        (r.code, r.env_var) for r in REFERENCE_REQUIREMENTS
    ]
    assert [v.setting for v in found] == [r.setting for r in REFERENCE_REQUIREMENTS]


@needs_docs
def test_the_env_example_on_the_page_passes_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    text = PAGE.read_text(encoding="utf-8")
    block = text.split("```ini\n", 1)[1].split("```", 1)[0]
    environ = dict(
        line.split("=", 1)
        for line in block.splitlines()
        if line.strip() and not line.startswith("#")
    )
    monkeypatch.delenv("CALIBER_FAULT_BARRIERS", raising=False)
    config = CaliberConfig.load(environ=environ)
    assert config.profile == "reference"
    assert config.admin_users and config.approver_users, (
        "a usable deployment names its admin and approver"
    )
    assert environ["CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV"], (
        "the bootstrap password source is named"
    )
    assert reference_profile_violations(config) == []


@needs_docs
def test_the_page_is_registered_in_the_docs_site() -> None:
    manifest = (PAGE.parents[2] / "docs-site" / "build-docs.mjs").read_text(encoding="utf-8")
    assert 'md: "operate/reference-profile.md"' in manifest
    assert "stability: beta" in PAGE.read_text(encoding="utf-8").split("---", 2)[1]
