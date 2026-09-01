"""Tests for the secret-source resolver.

Two layers:

1. Pure resolution by scheme: bare string (backwards compat),
   ``env://``, ``file://``.
2. Integration through the three call sites that consume the
   resolver: ``build_token_manager`` (CSRF), ``build_dispatcher``
   (webhooks), and ``build_provider`` (LLM API key).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caliber.config import CaliberConfig
from caliber.csrf import build_token_manager
from caliber.events.bus import EventBus
from caliber.events.webhooks import build_dispatcher
from caliber.llm.circuit_breaker import CircuitBreakerLLMProvider
from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
from caliber.llm.provider import LLMProviderError, build_provider
from caliber.secrets import resolve_secret

# ---------------------------------------------------------------------------
# Pure resolution
# ---------------------------------------------------------------------------


def test_empty_source_returns_none() -> None:
    """An empty source string short-circuits; no env / file lookup."""
    assert resolve_secret("", environ={"FOO": "bar"}) is None


def test_bare_string_treated_as_env_var_name() -> None:
    """Backwards compatibility: a bare string with no scheme is still
    the name of an env var, matching the pre-resolver behavior."""
    assert resolve_secret("FOO", environ={"FOO": "bar"}) == "bar"


def test_bare_string_missing_env_returns_none() -> None:
    """A missing/empty env var resolves to ``None`` so callers can
    treat it the same as 'no source configured'."""
    assert resolve_secret("MISSING", environ={}) is None
    assert resolve_secret("EMPTY", environ={"EMPTY": ""}) is None


def test_env_scheme_explicit() -> None:
    """``env://VAR`` is equivalent to the bare-string form, just explicit."""
    assert resolve_secret("env://FOO", environ={"FOO": "bar"}) == "bar"


def test_env_scheme_missing_returns_none() -> None:
    assert resolve_secret("env://MISSING", environ={}) is None


def test_env_scheme_empty_var_name_returns_none() -> None:
    """``env://`` with no name is malformed — returns ``None`` rather
    than silently treating the empty key as a valid lookup."""
    assert resolve_secret("env://", environ={"": "weird"}) is None


def test_file_scheme_reads_contents(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("hunter2", encoding="utf-8")
    assert resolve_secret(f"file://{secret_file}") == "hunter2"


def test_file_scheme_strips_trailing_whitespace(tmp_path: Path) -> None:
    """A trailing newline from ``echo "..." > file`` shouldn't end up
    in the secret — it would silently break HMAC comparisons."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("hunter2\n\n", encoding="utf-8")
    assert resolve_secret(f"file://{secret_file}") == "hunter2"


def test_file_scheme_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing file is treated like a missing env var: ``None``, no
    crash. The caller's enabled-but-empty branch fires."""
    missing = tmp_path / "nope"
    assert resolve_secret(f"file://{missing}") is None


def test_file_scheme_empty_file_returns_none(tmp_path: Path) -> None:
    """An empty (or whitespace-only) file is treated as 'no secret'."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("   \n\n", encoding="utf-8")
    assert resolve_secret(f"file://{secret_file}") is None


def test_file_scheme_empty_path_returns_none() -> None:
    """``file://`` with no path is malformed."""
    assert resolve_secret("file://") is None


def test_environ_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``environ`` is not passed, the resolver reads from
    :data:`os.environ` — the production code path."""
    monkeypatch.setenv("CALIBER_SECRET_TEST_KEY", "via-os-environ")
    assert resolve_secret("CALIBER_SECRET_TEST_KEY") == "via-os-environ"


# ---------------------------------------------------------------------------
# Integration: CSRF builder
# ---------------------------------------------------------------------------


def test_csrf_builder_resolves_bare_env_name() -> None:
    """Backwards compat: the existing ``*_env`` config field with a
    bare env-var name still works through the resolver."""
    manager = build_token_manager(
        enabled=True,
        secret_env_var="CSRF_TEST",
        ttl_seconds=3600,
        environ={"CSRF_TEST": "shhh"},
    )
    assert manager.is_enabled
    assert manager.secret == b"shhh"


def test_csrf_builder_resolves_file_uri(tmp_path: Path) -> None:
    """A file-mounted CSRF secret resolves through the same builder."""
    secret_file = tmp_path / "csrf.secret"
    secret_file.write_text("file-shhh", encoding="utf-8")
    manager = build_token_manager(
        enabled=True,
        secret_env_var=f"file://{secret_file}",
        ttl_seconds=3600,
        environ={},
    )
    assert manager.is_enabled
    assert manager.secret == b"file-shhh"


def test_csrf_builder_disabled_when_secret_unresolved() -> None:
    """An unresolvable source self-disables CSRF rather than running
    with an empty signing key."""
    manager = build_token_manager(
        enabled=True,
        secret_env_var="env://NOT_SET",
        ttl_seconds=3600,
        environ={},
    )
    assert not manager.is_enabled


# ---------------------------------------------------------------------------
# Integration: webhook dispatcher
# ---------------------------------------------------------------------------


def test_webhook_dispatcher_resolves_file_uri(tmp_path: Path) -> None:
    """A file-mounted webhook signing secret feeds into the dispatcher
    the same way an env-var-named secret does."""
    secret_file = tmp_path / "webhook.secret"
    secret_file.write_text("webhook-shhh", encoding="utf-8")
    dispatcher = build_dispatcher(
        bus=EventBus(),
        urls_csv="https://example.test/hook",
        secret_env_var=f"file://{secret_file}",
        event_filter_csv="*",
        environ={},
    )
    assert dispatcher.is_enabled


def test_webhook_dispatcher_disabled_when_secret_unresolved() -> None:
    """No URLs is one disable path; no secret is the other."""
    dispatcher = build_dispatcher(
        bus=EventBus(),
        urls_csv="https://example.test/hook",
        secret_env_var="env://NOT_SET",
        event_filter_csv="*",
        environ={},
    )
    assert not dispatcher.is_enabled


# ---------------------------------------------------------------------------
# Integration: LLM provider API key
# ---------------------------------------------------------------------------


def test_llm_provider_resolves_api_key_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm_api_key_env`` defaulting to bare ``OPENAI_API_KEY``
    still works after the resolver was wired in."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test-key")
    config = CaliberConfig.load(
        environ={
            "CALIBER_LLM_PROVIDER": "openai",
            "CALIBER_DATABASE_URL": "sqlite:///:memory:",
        }
    )
    provider = build_provider(config)
    # Default-on circuit breaker wraps the base provider.
    assert isinstance(provider, CircuitBreakerLLMProvider)


def test_llm_provider_resolves_api_key_from_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LLM API key sourced from a file (Kubernetes secret mount,
    Docker secret, Vault Agent template) loads cleanly."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    secret_file = tmp_path / "openai-key"
    secret_file.write_text("sk-fake-file-key", encoding="utf-8")
    config = CaliberConfig.load(
        environ={
            "CALIBER_LLM_PROVIDER": "openai",
            "CALIBER_DATABASE_URL": "sqlite:///:memory:",
            "CALIBER_LLM_API_KEY_ENV": f"file://{secret_file}",
            # Disable the breaker wrap so we can isinstance-check the
            # underlying provider directly.
            "CALIBER_LLM_CIRCUIT_BREAKER_ENABLED": "false",
        }
    )
    provider = build_provider(config)
    assert isinstance(provider, OpenAIAgentsLLMProvider)
    assert provider._gepa_reflection_model == "openai:/gpt-5.6-luna"
    assert provider._gepa_max_metric_calls == 100


def test_llm_provider_raises_when_api_key_source_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OpenAI provider fail-fasts when its API-key source can't be
    resolved — same as the pre-resolver behavior."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = CaliberConfig.load(
        environ={
            "CALIBER_LLM_PROVIDER": "openai",
            "CALIBER_DATABASE_URL": "sqlite:///:memory:",
        }
    )
    with pytest.raises(LLMProviderError, match=r"did not resolve"):
        build_provider(config)
