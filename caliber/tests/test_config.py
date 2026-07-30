"""Tests for the runtime configuration surface."""

from __future__ import annotations

import os

import pytest

from caliber.config import CaliberConfig, ConfigError


def test_defaults() -> None:
    config = CaliberConfig.load(environ={})
    assert config.log_level == "INFO"
    assert config.log_sink == "s3"
    assert config.log_bucket == "caliber-log"
    assert config.log_prefix == "service"
    assert config.log_s3_auto_create_bucket is True
    assert config.log_s3_flush_lines == 1
    assert config.static_prefix == ""
    assert config.dev_user == ""
    assert config.builtin_skills_auto_seed is False
    assert config.tool_sandbox_timeout_seconds == 5.0
    assert config.registered_tool_sandbox_timeout_seconds == 30.0
    assert config.tool_sandbox_max_output_bytes == 1_048_576
    assert config.tool_sandbox_max_memory_bytes == 268_435_456
    assert config.mcp_stdio_command_allowlist == "${PYTHON}"
    assert config.mcp_stdio_safe_path == os.defpath
    assert config.mcp_stdio_isolation_profile == "none"
    assert config.mcp_require_external_isolation_for_aliases == "prod"


def test_log_level_from_env() -> None:
    config = CaliberConfig.load(environ={"CALIBER_LOG_LEVEL": "debug"})
    assert config.log_level == "DEBUG"


def test_s3_log_settings_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_LOG_SINK": "S3",
            "CALIBER_LOG_BUCKET": "caliber-log-prod",
            "CALIBER_LOG_PREFIX": "api",
            "CALIBER_LOG_S3_AUTO_CREATE_BUCKET": "false",
            "CALIBER_LOG_S3_FLUSH_LINES": "25",
        }
    )
    assert config.log_sink == "s3"
    assert config.log_bucket == "caliber-log-prod"
    assert config.log_prefix == "api"
    assert config.log_s3_auto_create_bucket is False
    assert config.log_s3_flush_lines == 25


def test_static_prefix_from_env() -> None:
    config = CaliberConfig.load(environ={"CALIBER_STATIC_PREFIX": "/mlflow"})
    assert config.static_prefix == "/mlflow"


def test_dev_user_from_env() -> None:
    config = CaliberConfig.load(environ={"CALIBER_DEV_USER": "@local-admin"})
    assert config.dev_user == "@local-admin"


def test_tool_sandbox_settings_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_TOOL_SANDBOX_TIMEOUT_SECONDS": "1.5",
            "CALIBER_TOOL_SANDBOX_MAX_OUTPUT_BYTES": "4096",
            "CALIBER_TOOL_SANDBOX_MAX_MEMORY_BYTES": "67108864",
            "CALIBER_TOOL_SANDBOX_MAX_FILE_BYTES": "2048",
            "CALIBER_TOOL_SANDBOX_MAX_OPEN_FILES": "24",
        }
    )
    assert config.tool_sandbox_timeout_seconds == 1.5
    assert config.tool_sandbox_max_output_bytes == 4096
    assert config.tool_sandbox_max_memory_bytes == 67_108_864
    assert config.tool_sandbox_max_file_bytes == 2048
    assert config.tool_sandbox_max_open_files == 24


def test_registered_tool_timeout_matches_the_request_contract() -> None:
    config = CaliberConfig(registered_tool_sandbox_timeout_seconds=120.0)
    assert config.registered_tool_sandbox_timeout_seconds == 120.0

    with pytest.raises(ValueError, match="less than or equal to 120"):
        CaliberConfig(registered_tool_sandbox_timeout_seconds=120.1)


def test_mcp_containment_settings_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST": "${PYTHON},npx",
            "CALIBER_MCP_STDIO_SAFE_PATH": "/trusted/bin",
            "CALIBER_MCP_REMOTE_HOST_ALLOWLIST": "mcp.example.test",
            "CALIBER_MCP_MANAGED_SIDECAR_HOSTS": "mcp.example.test",
            "CALIBER_MCP_ALLOW_INSECURE_HTTP": "true",
            "CALIBER_MCP_STDIO_ISOLATED_WORKDIR": "false",
            "CALIBER_MCP_STDIO_ISOLATION_PROFILE": "bubblewrap",
            "CALIBER_MCP_STDIO_ISOLATION_PREFIX": "bwrap --unshare-all",
            "CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES": "prod,regulated",
        }
    )
    assert config.mcp_stdio_command_allowlist == "${PYTHON},npx"
    assert config.mcp_stdio_safe_path == "/trusted/bin"
    assert config.mcp_remote_host_allowlist == "mcp.example.test"
    assert config.mcp_managed_sidecar_hosts == "mcp.example.test"
    assert config.mcp_allow_insecure_http is True
    assert config.mcp_stdio_isolated_workdir is False
    assert config.mcp_stdio_isolation_profile == "bubblewrap"
    assert config.mcp_require_external_isolation_for_aliases == "prod,regulated"


def test_database_url_from_env_normalizes_postgres_driver() -> None:
    config = CaliberConfig.load(
        environ={"CALIBER_DATABASE_URL": "postgresql://caliber:secret@db-host:5433/caliberdb"}
    )

    assert config.database_url == "postgresql+psycopg://caliber:secret@db-host:5433/caliberdb"


def test_database_url_from_env_preserves_explicit_driver() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": "postgresql+asyncpg://caliber:secret@db-host:5433/caliberdb"
        }
    )

    assert config.database_url == "postgresql+asyncpg://caliber:secret@db-host:5433/caliberdb"


def test_gepa_settings_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_GEPA_REFLECTION_MODEL": "openai:/gpt-4.1",
            "CALIBER_GEPA_MAX_METRIC_CALLS": "250",
        }
    )
    assert config.gepa_reflection_model == "openai:/gpt-4.1"
    assert config.gepa_max_metric_calls == 250


def test_nats_event_backend_settings_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_WORKFLOW_RUN_EVENT_BACKEND": "nats",
            "CALIBER_NATS_URL": "nats://nats:4222,nats://nats-2:4222",
            "CALIBER_NATS_SUBJECT": "caliber.events.prod",
        }
    )
    assert config.workflow_run_event_backend == "nats"
    assert config.nats_url == "nats://nats:4222,nats://nats-2:4222"
    assert config.nats_subject == "caliber.events.prod"


def test_redis_event_backend_settings_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_WORKFLOW_RUN_EVENT_BACKEND": "redis",
            "CALIBER_REDIS_URL": "redis://cache:6379/4",
            "CALIBER_REDIS_CHANNEL": "caliber.events.live",
        }
    )
    assert config.workflow_run_event_backend == "redis"
    assert config.redis_url == "redis://cache:6379/4"
    assert config.redis_channel == "caliber.events.live"


def test_builtin_skills_auto_seed_from_env() -> None:
    config = CaliberConfig.load(environ={"CALIBER_BUILTIN_SKILLS_AUTO_SEED": "true"})
    assert config.builtin_skills_auto_seed is True


def test_flagged_optional_stack_overrides_from_env() -> None:
    config = CaliberConfig.load(
        environ={
            "CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS": "true",
            "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS": "true",
        }
    )
    assert config.allow_flagged_dspy_optimizers is True
    assert config.allow_flagged_local_embeddings is True


def test_invalid_log_level_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="invalid CALIBER configuration"):
        CaliberConfig.load(environ={"CALIBER_LOG_LEVEL": "TRACE"})


def test_invalid_log_sink_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="invalid CALIBER configuration"):
        CaliberConfig.load(environ={"CALIBER_LOG_SINK": "kinesis"})


def test_config_is_frozen() -> None:
    config = CaliberConfig.load(environ={})
    # Pydantic v2 raises ValidationError ("Instance is frozen") on assignment when frozen=True.
    with pytest.raises(Exception, match=r"(?i)frozen"):
        config.log_level = "DEBUG"  # type: ignore[misc]


def test_unrecognized_fields_rejected() -> None:
    # ``extra="forbid"`` in ConfigDict means passing unknown fields fails
    # rather than silently being ignored — important for typo prevention.
    # CaliberConfig.load() only reads documented env vars, so the rejection
    # surface is direct construction:
    with pytest.raises(Exception, match=r"(?i)extra"):
        CaliberConfig(unknown="x")  # type: ignore[call-arg]


def test_flag_accepts_common_truthy_spellings() -> None:
    """Regression (#22): boolean env flags must accept the usual truthy
    spellings, not only the exact string "true" (a "=1"/"=yes" used to silently
    disable fail-safe defaults)."""
    from caliber.config import _flag

    assert all(_flag(v) for v in ["true", "True", "TRUE", "1", "yes", "on", " on "])
    assert not any(_flag(v) for v in ["false", "0", "no", "off", "", "nope"])
