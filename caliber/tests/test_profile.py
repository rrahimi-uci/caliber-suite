"""The reference deployment profile refuses to start on an unsafe configuration (EGI B12 part 1).

``CALIBER_PROFILE=reference`` makes ``create_app`` raise ``ConfigError`` listing every setting that
breaks the profile, each with the environment variable to change. The default ``development`` profile
must keep starting with the very same settings, so a local install is unaffected.

A *safe* reference configuration needs PostgreSQL, which the default test jobs do not have. The
refusal tests do not need a database at all (the check runs before one is opened), and the one test
that starts a safe reference app runs against the PostgreSQL service of the migration-parity CI job.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.effects.dispatcher as dispatcher_module
import caliber.profile as profile_module
import tests.test_effect_dispatcher as dispatcher_tests
from caliber.config import CaliberConfig, ConfigError
from caliber.db.migrate import build_config
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEventOutbox,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects import admission
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import (
    EffectRefused,
    TransportResult,
    UnsupportedEffectPath,
    dispatch,
    submit_proposal,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import Intent, proposal_state, record_proposal
from caliber.profile import (
    REFERENCE_CONNECTORS,
    ProfileViolation,
    apply_distinct_approver_required,
    reference_profile_violations,
)
from caliber.server import create_app
from tests.dialect_helpers import POSTGRES_TEST_URL, dialect_database_url
from tests.effect_helpers import admit_fake_backend, leased
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database

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
    "apply_require_distinct_approver": True,
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
    "apply_distinct_approver_disabled": (
        {"apply_require_distinct_approver": False},
        "CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER",
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


def test_every_violation_in_order(app_config: CaliberConfig) -> None:
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


def test_apply_flag_is_a_reference_violation(app_config: CaliberConfig) -> None:
    """B06.3 (#747): the reference profile refuses to start with the two-person Apply rule off."""
    off = _config(app_config, apply_require_distinct_approver=False)
    (violation,) = reference_profile_violations(off)
    assert violation.code == "apply_distinct_approver_disabled"
    assert violation.setting == "apply_require_distinct_approver"
    assert violation.env_var == "CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER"
    with pytest.raises(ConfigError) as refused:
        create_app(off)
    assert "CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER=true" in str(refused.value)
    assert reference_profile_violations(_config(app_config)) == []


def test_apply_flag_defaults_off_and_is_read_from_the_environment() -> None:
    """The development profile keeps today's behaviour unless an operator opts in."""
    assert CaliberConfig.load(environ={}).apply_require_distinct_approver is False
    on = CaliberConfig.load(environ={"CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER": "true"})
    assert on.apply_require_distinct_approver is True
    # Like every flag here, an unrecognised spelling is false: the reference profile then refuses it.
    unrecognised = CaliberConfig.load(environ={"CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER": "maybe"})
    assert unrecognised.apply_require_distinct_approver is False


def test_the_development_profile_does_not_flag_the_apply_setting(app_config: CaliberConfig) -> None:
    """The profile check is only run for ``reference``; development starts with the flag off."""
    off = app_config.model_copy(
        update={"profile": "development", "apply_require_distinct_approver": False}
    )
    assert TestClient(create_app(off)).get(HEALTH).status_code == 200


@pytest.mark.parametrize(
    ("profile", "flag", "required"),
    [
        ("development", False, False),
        ("development", True, True),
        ("reference", True, True),
        # The startup check refuses this pair; the runtime rule stays closed if a process is in it anyway.
        ("reference", False, True),
    ],
)
def test_apply_distinct_approver_required_matrix(profile: str, flag: bool, required: bool) -> None:
    config = CaliberConfig.model_construct(profile=profile, apply_require_distinct_approver=flag)
    assert apply_distinct_approver_required(config) is required


def test_apply_distinct_approver_required_is_closed_without_a_config() -> None:
    assert apply_distinct_approver_required(None) is True


def test_a_truthy_non_boolean_flag_does_not_switch_the_rule_on() -> None:
    """Only a real ``True`` counts: an object that merely looks truthy on a development config is not the flag."""
    config = SimpleNamespace(profile="development", apply_require_distinct_approver="yes")
    assert apply_distinct_approver_required(config) is False


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
    "apply_distinct_approver_disabled": (
        "apply_require_distinct_approver",
        "apply_require_distinct_approver is false; "
        "set CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER=true for the reference profile",
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
    assert lines[0] == "reference profile refused (10 violations):"
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


# ---------------------------------------------------------------- trusted connector admission (B12.3a, #738)
#
# A connector is admitted by the registered transport's implementation identity, its capability record and its wire
# configuration, each compared with the pinned record in ``REFERENCE_CONNECTORS``; never by a tool, connector or backend
# *name* a caller supplies. These tests drive the real dispatcher (SQLite) with a fake connector whose call count proves
# that a refusal happens before anything is claimed or sent.


class _FakeConnector:
    """A transport that records every call, so a refusal can be shown to precede any send."""

    def __init__(self) -> None:
        self.calls = 0

    def perform(self, _request: Any, _config: Any) -> Any:
        self.calls += 1
        raise AssertionError("an unadmitted connector must never be called")


class _Backend:
    """Same name as the real document-request transport, different module: a lookalike."""

    def perform(self, _request: Any, _config: Any) -> Any:  # pragma: no cover - never reached
        raise AssertionError("a lookalike transport must never be called")


class _SubclassOfReal(dispatcher_module._Backend):
    """A subclass of the real transport: a different implementation identity."""


def _doc_admit_args(**overrides: Any) -> dict[str, Any]:
    registration = dispatcher_module.BACKENDS["document_request"]
    args: dict[str, Any] = {
        "backend": registration.backend,
        "connector": registration.connector,
        "connector_version": registration.connector_version,
        "capability": registration.capability,
        "configuration": registration.configuration,
    }
    args.update(overrides)
    return args


def _admit(**overrides: Any) -> admission.AdmittedConnector:
    return admission.admit(REFERENCE_CONNECTORS, "document_request", **_doc_admit_args(**overrides))


def test_the_trusted_table_pins_exactly_the_registered_backends() -> None:
    assert [t.backend_kind for t in REFERENCE_CONNECTORS] == list(dispatcher_module.BACKENDS)
    for record in REFERENCE_CONNECTORS:
        registration = dispatcher_module.BACKENDS[record.backend_kind]
        assert registration.capability is not None and registration.configuration is not None
        assert record.implementation == admission.implementation_identity(registration.backend)
        assert record.capability_digest == registration.capability.digest()
        assert record.configuration_digest == admission.configuration_digest(
            registration.configuration
        )
        # Accepted risks are exactly the capability's: a stale acceptance would hide a fixed weakness.
        assert set(record.accepted_risks) == set(
            admission.capability_risks(registration.capability)
        )
        assert (record.connector, record.connector_version) == (
            registration.connector,
            registration.connector_version,
        )
        assert set(record.required_config_keys) <= set(record.config_keys)
        assert set(record.config_keys) <= set(admission._CONFIG_CHECKS)
        admitted = dispatcher_module.admit_backend(record.backend_kind)
        assert admitted.evidence()["implementation"] == record.implementation


def test_the_reference_connector_is_admitted_and_nothing_else_is_listed() -> None:
    admitted = dispatcher_module.admit_backend("document_request")
    assert (admitted.connector, admitted.connector_version) == ("document-request", "1")
    assert admitted.accepted_risks == ("key_retention_unconfirmed",)
    # MCP stdio and remote hosts, host-file exports and PostgreSQL MCP writes have no record, so they are refused.
    listed = {name for t in REFERENCE_CONNECTORS for name in (t.backend_kind, t.connector)}
    assert listed == {"document_request", "document-request", "http"}
    assert profile_module.reserved_connector_names() == listed


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"connector": "http"}, admission.NOT_ADMITTED),
        ({"connector_version": "2"}, admission.NOT_ADMITTED),
        ({"backend": _Backend()}, admission.IMPLEMENTATION_MISMATCH),
        ({"backend": _SubclassOfReal()}, admission.IMPLEMENTATION_MISMATCH),
        ({"backend": lambda _r, _c: None}, admission.IMPLEMENTATION_MISMATCH),
        ({"capability": None}, admission.CAPABILITY_UNPROVEN),
        ({"capability": {"connector": "document-request"}}, admission.CAPABILITY_UNPROVEN),
        (
            {
                "capability": replace(
                    dispatcher_module._document_request.CAPABILITY, key_scope="resource"
                )
            },
            admission.CAPABILITY_MISMATCH,
        ),
        (
            {"capability": replace(dispatcher_module._document_request.CAPABILITY, version="2")},
            admission.CAPABILITY_MISMATCH,
        ),
        ({"configuration": None}, admission.CONFIGURATION_MISMATCH),
        ({"configuration": []}, admission.CONFIGURATION_MISMATCH),
        (
            {
                "configuration": {
                    **dispatcher_module.DOCUMENT_REQUEST_CONFIGURATION,
                    "resource": "^anything$",
                }
            },
            admission.CONFIGURATION_MISMATCH,
        ),
    ],
    ids=[
        "other-connector",
        "other-version",
        "lookalike-class",
        "subclass",
        "bare-function",
        "no-capability",
        "capability-not-a-record",
        "changed-promise",
        "capability-for-another-version",
        "no-configuration",
        "configuration-not-a-mapping",
        "changed-wire-configuration",
    ],
)
def test_admission_refuses_each_unpinned_property_with_a_typed_code(
    overrides: dict[str, Any], code: str
) -> None:
    with pytest.raises(admission.AdmissionRefused) as refused:
        _admit(**overrides)
    assert refused.value.code == code
    assert str(refused.value) == code, "a refusal carries its code and nothing else"


def test_admission_refuses_an_unknown_kind_and_an_unaccepted_risk() -> None:
    with pytest.raises(admission.AdmissionRefused) as refused:
        admission.admit(REFERENCE_CONNECTORS, "mcp_stdio", **_doc_admit_args())
    assert refused.value.code == admission.NOT_ADMITTED
    (record,) = [t for t in REFERENCE_CONNECTORS if t.backend_kind == "document_request"]
    with pytest.raises(admission.AdmissionRefused) as unaccepted:
        admission.admit(
            (replace(record, accepted_risks=()),), "document_request", **_doc_admit_args()
        )
    assert unaccepted.value.code == admission.CAPABILITY_UNPROVEN
    assert _admit().backend_kind == "document_request"


def test_every_refusal_code_is_distinct() -> None:
    assert len(set(admission.REFUSAL_CODES)) == len(admission.REFUSAL_CODES) == 6


@needs_docs
def test_every_refusal_code_is_documented_on_the_operator_page() -> None:
    page = PAGE.read_text(encoding="utf-8")
    for code in (*admission.REFUSAL_CODES, "unsupported_backend", "unsupported_effect_path"):
        assert f"`{code}`" in page, f"{code} is not documented on the operator page"


def test_capability_risks_are_named_exactly_when_they_are_present() -> None:
    from caliber.effects.connectors import ConnectorCapability

    proven = ConnectorCapability(
        connector="proven",
        version="1",
        idempotency_key_header="Idempotency-Key",
        key_scope="downstream",
        key_retention="bounded",
        key_retention_seconds=86400,
        key_conflict_status=422,
        precondition_header="If-Match",
        stale_precondition_status=412,
        definitive_rejection_statuses=frozenset({412, 422}),
        lookup_by_key=True,
        lookup_consistency="read_your_writes",
        absence_proves_noncommit=False,
    )
    assert admission.capability_risks(proven) == ()
    unconfirmed = replace(proven, key_retention="unconfirmed", key_retention_seconds=None)
    assert admission.capability_risks(unconfirmed) == (admission.RISK_RETENTION_UNCONFIRMED,)
    assert admission.capability_risks(dispatcher_module.HTTP_CAPABILITY) == (
        admission.RISK_NOT_EXACTLY_ONCE,
        admission.RISK_RETENTION_UNCONFIRMED,
    )


def test_implementation_identity_is_the_class_or_the_function_not_a_claimed_name() -> None:
    real = dispatcher_module.BACKENDS["document_request"].backend
    assert admission.implementation_identity(real) == "caliber.effects.dispatcher:_Backend"
    assert admission.implementation_identity(_Backend()) == f"{__name__}:_Backend"
    assert admission.implementation_identity(_doc_admit_args) == f"{__name__}:_doc_admit_args"


def _document_admitted() -> admission.AdmittedConnector:
    return dispatcher_module.admit_backend("document_request")


def _http_admitted() -> admission.AdmittedConnector:
    return dispatcher_module.admit_backend("http")


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": "https://downstream.example.test"},
        {"base_url": "http://127.0.0.1:8080/api", "max_attempts": 1, "timeout_seconds": 0.5},
        {"base_url": "https://d.example.test", "max_attempts": 5, "timeout_seconds": 300},
        {"base_url": "https://d.example.test", "timeout_seconds": 300.0},
    ],
)
def test_check_config_accepts_bounded_document_request_settings(config: dict[str, Any]) -> None:
    admission.check_config(_document_admitted(), config)


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"max_attempts": 2},
        {"base_url": "https://d.example.test", "headers": {"X": "y"}},
        {"base_url": "https://d.example.test", "sender": print},
        {"base_url": ""},
        {"base_url": "   "},
        {"base_url": 7},
        {"base_url": "ftp://d.example.test"},
        {"base_url": "file:///etc/passwd"},
        {"base_url": "https://user:pw@d.example.test"},
        {"base_url": "https://d.example.test/x?token=1"},
        {"base_url": "https://d.example.test/x#frag"},
        {"base_url": "https:///nohost"},
        {"base_url": "http://[::1"},
        {"base_url": "https://d.example.test", "max_attempts": 0},
        {"base_url": "https://d.example.test", "max_attempts": 6},
        {"base_url": "https://d.example.test", "max_attempts": True},
        {"base_url": "https://d.example.test", "max_attempts": "2"},
        {"base_url": "https://d.example.test", "max_attempts": 1.5},
        {"base_url": "https://d.example.test", "timeout_seconds": 0},
        {"base_url": "https://d.example.test", "timeout_seconds": -1},
        {"base_url": "https://d.example.test", "timeout_seconds": float("nan")},
        {"base_url": "https://d.example.test", "timeout_seconds": float("inf")},
        {"base_url": "https://d.example.test", "timeout_seconds": 300.01},
        {"base_url": "https://d.example.test", "timeout_seconds": "5"},
        {"base_url": "https://d.example.test", "timeout_seconds": True},
        # An int too large to convert to float is refused with the typed code, not an OverflowError.
        {"base_url": "https://d.example.test", "timeout_seconds": 10**400},
        {"base_url": "https://d.example.test", "timeout_seconds": -(10**400)},
        # Ports are parsed: non-numeric and out-of-range ports are refused.
        {"base_url": "https://d.example.test:99999"},
        {"base_url": "http://h.com:abc"},
        {"base_url": "http://h.com:-1"},
        # Whitespace and control characters anywhere, including leading and trailing.
        {"base_url": " https://d.example.test"},
        {"base_url": "https://d.example.test "},
        {"base_url": "https://d.example.test\n"},
        {"base_url": "https://d.example.test\r\nHost: evil"},
        {"base_url": "https://d .example.test"},
        {"base_url": "https://d.example.test/a\tb"},
        {"base_url": "https://d.example.test\x00"},
        {"base_url": "https://d.example.test\u00a0"},
        # Userinfo in any spelling, including an empty one.
        {"base_url": "https://@d.example.test"},
        {"base_url": "https://user@d.example.test"},
        {"base_url": "https://:pw@d.example.test"},
        {"base_url": "https://d.example.test@evil.example"},
    ],
)
def test_check_config_refuses_unbounded_or_unknown_document_request_settings(
    config: dict[str, Any] | None,
) -> None:
    with pytest.raises(admission.AdmissionRefused) as refused:
        admission.check_config(_document_admitted(), config)
    assert refused.value.code == admission.CONFIG_NOT_ADMITTED
    assert str(refused.value) == admission.CONFIG_NOT_ADMITTED


def test_check_config_accepts_the_edge_values_of_a_valid_base_url_and_timeout() -> None:
    for config in (
        {"base_url": "https://d.example.test:65535/api/v1"},
        {"base_url": "http://[::1]:8080"},
        {"base_url": "http://127.0.0.1:1"},
        {"base_url": "https://d.example.test", "timeout_seconds": 1},
    ):
        admission.check_config(_document_admitted(), config)


def test_the_timeout_check_itself_never_raises_on_huge_numbers() -> None:
    """Refused by the check, not rescued by the guard around it: ``float(10**400)`` is an OverflowError."""
    assert admission._valid_timeout(10**400) is False
    assert admission._valid_timeout(-(10**400)) is False
    assert admission._valid_timeout(float("1e308")) is False
    assert admission._valid_timeout(300) is True


def test_a_check_that_cannot_decide_refuses_with_the_typed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_value: object) -> bool:
        raise RuntimeError("a check bug")

    monkeypatch.setitem(admission._CONFIG_CHECKS, "base_url", explode)
    with pytest.raises(admission.AdmissionRefused) as refused:
        admission.check_config(_document_admitted(), {"base_url": "https://d.example.test"})
    assert refused.value.code == admission.CONFIG_NOT_ADMITTED


def test_check_config_for_the_generic_transport_needs_both_callables_and_nothing_more() -> None:
    admission.check_config(_http_admitted(), {"sender": print, "receipt_verifier": print})
    for config in (
        None,
        {},
        {"sender": print},
        {"sender": print, "receipt_verifier": "verify"},
        {"sender": print, "receipt_verifier": print, "base_url": "https://d.example.test"},
        [("sender", print)],
    ):
        with pytest.raises(admission.AdmissionRefused) as refused:
            admission.check_config(_http_admitted(), config)  # type: ignore[arg-type]
        assert refused.value.code == admission.CONFIG_NOT_ADMITTED


def test_a_record_cannot_list_a_setting_nobody_validates() -> None:
    (record,) = [t for t in REFERENCE_CONNECTORS if t.backend_kind == "document_request"]
    loose = replace(record, config_keys=(*record.config_keys, "execution_config"))
    admitted = admission.admit((loose,), "document_request", **_doc_admit_args())
    with pytest.raises(admission.AdmissionRefused) as refused:
        admission.check_config(
            admitted, {"base_url": "https://d.example.test", "execution_config": {}}
        )
    assert refused.value.code == admission.CONFIG_NOT_ADMITTED


# -- the real dispatcher, a fake connector and a real (SQLite) ledger


def _record(
    session: Session, *, connector: str, version: str = "1", occurrence: str = "occ-adm"
) -> str:
    """A proposal row for ``connector``, written under the dispatcher's own checks (as legacy or foreign data)."""
    recorded = record_proposal(
        session,
        workflow_run_id="RUN-ep",
        node_id="agent",
        occurrence_id=occurrence,
        intent=Intent(
            connector=connector,
            connector_version=version,
            effect_class="irreversible_write",
            resource="cases/CASE-1/document-requests",
            args={"document_type": "pay_statement"},
            expected_resource_version="1",
        ),
    )
    session.commit()
    return recorded.proposal_id


def _proposal_count(factory: Any) -> int:
    with factory() as session:
        return len(session.scalars(select(CaliberEffectProposal.proposal_id)).all())


#: kind -> (tool name, caliber_defined, connector the proposal names, the code a bare proposal is refused with)
UNTRUSTED_KINDS: dict[str, tuple[str, bool, str, str]] = {
    "spoofed_document_request_tool": (
        "document_request",
        False,
        "document-request",
        "connector_not_admitted",
    ),
    "unregistered_mcp": ("invoke_mcp_tool", False, "mcp-stdio", "connector_not_admitted"),
    "host_file_export": ("read_text_file", True, "host-file", "connector_not_admitted"),
    "database_mcp_write": ("create_table", False, "postgres-mcp", "connector_not_admitted"),
}


@pytest.mark.parametrize("kind", list(UNTRUSTED_KINDS))
def test_untrusted_connector_refused(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the four untrusted kinds is refused before a proposal exists and before anything is sent.

    A name (tool, connector or backend kind) confers nothing: ``submit_proposal`` needs an admission the dispatcher
    verified against the live registry, and ``dispatch`` admits the registered transport itself, so even a transport
    someone added to ``BACKENDS`` without a pinned record is never called.
    """
    tool_name, caliber_defined, connector, code = UNTRUSTED_KINDS[kind]
    fake = _FakeConnector()
    trusted = admit_fake_backend(monkeypatch, "reviewed-fake", fake, connector="reviewed-connector")
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", dispatcher_tests._validator)
    claimed = {
        "connector": connector,
        "connector_version": "1",
        "effect_class": "read",
        "resource": "cases/CASE-1/document-requests",
        "args": {"document_type": "pay_statement"},
    }
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        submit: dict[str, Any] = {
            "workflow_run_id": "RUN-ep",
            "node_id": "agent",
            "occurrence_id": "occ-untrusted",
            "action": claimed,
            "tool_name": tool_name,
            "caliber_defined": caliber_defined,
        }
        # 1. The claim alone is not an admission.
        with factory() as session, pytest.raises(UnsupportedEffectPath) as bare:
            submit_proposal(session, **submit)
        assert bare.value.code == code
        # 2. An admission for a *reviewed* connector does not carry over to the connector the action names.
        with factory() as session, pytest.raises(UnsupportedEffectPath) as stolen:
            submit_proposal(session, **submit, admission=trusted)
        assert stolen.value.code == code
        # 3. A hand-built admission whose evidence differs from the live registry's is refused. (An *equal* hand-built one
        #    is accepted: it carries no secret, so this is not an unforgeable capability; see the operator page.)
        (real,) = [t for t in REFERENCE_CONNECTORS if t.backend_kind == "document_request"]
        forged = admission.AdmittedConnector(
            backend_kind="document_request",
            connector=connector,
            connector_version="1",
            implementation="builtins:object",
            capability_digest=real.capability_digest,
            configuration_digest=real.configuration_digest,
            accepted_risks=real.accepted_risks,
            config_keys=real.config_keys,
            required_config_keys=real.required_config_keys,
        )
        with factory() as session, pytest.raises(UnsupportedEffectPath) as faked:
            submit_proposal(session, **submit, admission=forged)
        assert faked.value.code == code
        assert _proposal_count(factory) == 0, (
            "a refused proposal leaves no row, so no approval can follow"
        )

        # 4. A proposal row for that connector that exists anyway (foreign or legacy data) is never dispatched, even
        #    through a transport someone put in BACKENDS without a pinned record.
        with factory() as session:
            proposal_id = _record(session, connector=connector)
            approval_id = dispatcher_tests._approval(session, proposal_id)
            lease = leased(session, "admission-dispatcher")
            session.commit()
        monkeypatch.setitem(
            dispatcher_module.BACKENDS,
            "unreviewed",
            dispatcher_module.BackendRegistration(fake, connector, "1"),
        )
        for backend_kind, expected in (
            ("unreviewed", "connector_not_admitted"),
            ("mcp_stdio", "unsupported_backend"),
            (kind, "unsupported_backend"),
            # The real, admitted generic transport refuses a proposal that names another connector, unclaimed.
            ("http", "backend_intent_mismatch"),
        ):
            with pytest.raises(EffectRefused) as refused:
                dispatch(
                    factory,
                    proposal_id,
                    approval_id=approval_id,
                    approver_scopes={"caliber.approver"},
                    lease=lease,
                    backend_kind=backend_kind,
                    backend_config={"sender": print, "receipt_verifier": print},
                    approval_validator=dispatcher_tests._validator,
                    now=dispatcher_tests.AT,
                )
            assert refused.value.code == expected
        assert fake.calls == 0
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"
            approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
            assert approval is not None and approval.consumed_at is None
            assert session.scalars(select(CaliberEventOutbox.event_id)).all() == []


def test_a_host_file_tool_is_refused_by_the_inventory_even_with_an_admitted_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission is necessary, not sufficient: EP-055 (host files) and EP-114 (database MCP) stay ``refuse``."""
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        for tool in ("read_text_file", "grep_files"):
            with factory() as session, pytest.raises(UnsupportedEffectPath) as refused:
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id=f"occ-{tool}",
                    connector="document-request",
                    connector_version="1",
                    effect_class_name="read",
                    resource="cases/CASE-1/document-requests",
                    args={"document_type": "pay_statement"},
                    tool_name=tool,
                    caliber_defined=True,
                    admission=dispatcher_module.admit_backend("document_request"),
                )
            assert refused.value.code == "unsupported_effect_path"
        assert _proposal_count(factory) == 0


def test_a_registered_fake_connector_commits_and_its_admission_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive path through the real dispatcher: admitted, claimed, sent once, and the admission is evidence."""
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", dispatcher_tests._validator)
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)

        class Connector:
            calls = 0

            def perform(self, request: Any, _config: Any) -> TransportResult:
                Connector.calls += 1
                view = request.view
                facts = ReceiptFacts(
                    attempt=view.attempt or 1,
                    idempotency_key=view.logical_key,
                    connector=view.connector,
                    connector_version=view.connector_version,
                    resource=view.resource,
                    intent_hash=view.intent_hash,
                    downstream_request_id="downstream-1",
                    response_digest="sha256:" + "a" * 64,
                )
                return TransportResult(
                    Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1),
                    lambda: facts,
                )

        backend = Connector()
        admitted = admit_fake_backend(
            monkeypatch, "reviewed-fake", backend, connector="reviewed-connector"
        )
        with factory() as session:
            proposal_id = submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-ok",
                connector="reviewed-connector",
                connector_version="1",
                effect_class_name="irreversible_write",
                resource="cases/CASE-1/document-requests",
                args={"document_type": "pay_statement"},
                admission=admitted,
            ).proposal_id
            session.commit()
            approval_id = dispatcher_tests._approval(session, proposal_id)
            lease = leased(session, "admission-dispatcher")
            session.commit()
        result = dispatch(
            factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes={"caliber.approver"},
            lease=lease,
            backend_kind="reviewed-fake",
            approval_validator=dispatcher_tests._validator,
            now=dispatcher_tests.AT,
        )
        assert result.state == "committed"
        assert Connector.calls == 1
        with factory() as session:
            payload = session.scalars(select(CaliberEventOutbox.payload)).one()
        assert payload["connector_admission"] == admitted.evidence()
        assert set(payload["connector_admission"]) == {
            "backend_kind",
            "connector",
            "connector_version",
            "implementation",
            "capability_digest",
            "configuration_digest",
        }


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("transport_swapped", "connector_implementation_mismatch"),
        ("capability_dropped", "connector_capability_unproven"),
        ("promise_changed", "connector_capability_mismatch"),
        ("wire_config_changed", "connector_configuration_mismatch"),
        ("record_withdrawn", "connector_not_admitted"),
    ],
)
def test_a_registration_changed_after_admission_is_refused_before_the_claim(
    tamper: str, code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission is re-checked at dispatch, not remembered: a swapped transport is never called and nothing is consumed."""
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", dispatcher_tests._validator)
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        fake = _FakeConnector()
        admitted = admit_fake_backend(
            monkeypatch, "reviewed-fake", fake, connector="reviewed-connector"
        )
        with factory() as session:
            proposal_id = submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-tamper",
                connector="reviewed-connector",
                connector_version="1",
                effect_class_name="irreversible_write",
                resource="cases/CASE-1/document-requests",
                args={"document_type": "pay_statement"},
                admission=admitted,
            ).proposal_id
            session.commit()
            approval_id = dispatcher_tests._approval(session, proposal_id)
            lease = leased(session, "admission-dispatcher")
            session.commit()
        registration = dispatcher_module.BACKENDS["reviewed-fake"]
        if tamper == "transport_swapped":
            replacement = replace(registration, backend=_SwapTarget(fake))
        elif tamper == "capability_dropped":
            replacement = replace(registration, capability=None)
        elif tamper == "promise_changed":
            assert registration.capability is not None
            replacement = replace(
                registration, capability=replace(registration.capability, key_scope="resource")
            )
        elif tamper == "wire_config_changed":
            replacement = replace(registration, configuration={"fake": "somewhere-else"})
        else:
            replacement = registration
            monkeypatch.setattr(
                profile_module,
                "REFERENCE_CONNECTORS",
                tuple(t for t in REFERENCE_CONNECTORS if t.backend_kind != "reviewed-fake"),
            )
        monkeypatch.setitem(dispatcher_module.BACKENDS, "reviewed-fake", replacement)
        with pytest.raises(UnsupportedEffectPath) as refused:
            dispatch(
                factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes={"caliber.approver"},
                lease=lease,
                backend_kind="reviewed-fake",
                approval_validator=dispatcher_tests._validator,
                now=dispatcher_tests.AT,
            )
        assert refused.value.code == code
        assert fake.calls == 0
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"
            approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
            assert approval is not None and approval.consumed_at is None
        # The admission captured at submit is no longer valid either.
        with factory() as session, pytest.raises(UnsupportedEffectPath):
            submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-tamper-2",
                connector="reviewed-connector",
                connector_version="1",
                effect_class_name="irreversible_write",
                resource="cases/CASE-1/document-requests",
                args={"document_type": "pay_statement"},
                admission=admitted,
            )


class _SwapTarget:
    """Stands in for a transport someone swapped in under an admitted kind."""

    def __init__(self, inner: _FakeConnector) -> None:
        self.inner = inner

    def perform(self, request: Any, config: Any) -> Any:
        return self.inner.perform(request, config)


def test_dispatch_refuses_settings_the_connector_does_not_accept_before_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad ``base_url`` is refused with the proposal ``proposed`` and its approval unconsumed (not burnt by a send)."""
    monkeypatch.setattr(dispatcher_module, "approval_valid_at_commit", dispatcher_tests._validator)
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            proposal_id = submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-config",
                connector="document-request",
                connector_version="1",
                effect_class_name="irreversible_write",
                resource="cases/CASE-1/document-requests",
                args={"document_type": "pay_statement"},
                admission=dispatcher_module.admit_backend("document_request"),
            ).proposal_id
            session.commit()
            approval_id = dispatcher_tests._approval(session, proposal_id)
            lease = leased(session, "admission-dispatcher")
            session.commit()
        for config in (
            None,
            {"base_url": "file:///etc/passwd"},
            {"base_url": "https://d.example.test", "x": 1},
        ):
            with pytest.raises(UnsupportedEffectPath) as refused:
                dispatch(
                    factory,
                    proposal_id,
                    approval_id=approval_id,
                    approver_scopes={"caliber.approver"},
                    lease=lease,
                    backend_kind="document_request",
                    backend_config=config,
                    approval_validator=dispatcher_tests._validator,
                    now=dispatcher_tests.AT,
                )
            assert refused.value.code == "connector_config_not_admitted"
        with factory() as session:
            assert proposal_state(session, proposal_id).state == "proposed"
            approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
            assert approval is not None and approval.consumed_at is None


def test_a_non_string_backend_kind_is_unsupported_not_a_crash() -> None:
    for kind in (None, 7, ["document_request"], ""):
        with pytest.raises(UnsupportedEffectPath) as refused:
            dispatcher_module.admit_backend(kind)
        assert refused.value.code == "unsupported_backend"


def test_a_proposal_for_the_real_connector_needs_the_admission_for_that_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reference connector's admission cannot carry a proposal for the generic HTTP connector or another version."""
    with _database("sqlite", tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        document = dispatcher_module.admit_backend("document_request")
        for connector, version in (
            ("http", "1"),
            ("document-request", "2"),
            ("document_request", "1"),
        ):
            with factory() as session, pytest.raises(UnsupportedEffectPath) as refused:
                submit_proposal(
                    session,
                    workflow_run_id="RUN-ep",
                    node_id="agent",
                    occurrence_id="occ-wrong",
                    connector=connector,
                    connector_version=version,
                    effect_class_name="irreversible_write",
                    resource="cases/CASE-1/document-requests",
                    args={"document_type": "pay_statement"},
                    admission=document,
                )
            assert refused.value.code == "connector_not_admitted"
        with factory() as session, pytest.raises(UnsupportedEffectPath) as not_an_admission:
            submit_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="agent",
                occurrence_id="occ-dict",
                connector="document-request",
                connector_version="1",
                effect_class_name="irreversible_write",
                resource="cases/CASE-1/document-requests",
                args={"document_type": "pay_statement"},
                admission={"backend_kind": "document_request"},  # type: ignore[arg-type]
            )
        assert not_an_admission.value.code == "connector_not_admitted"
        assert _proposal_count(factory) == 0
