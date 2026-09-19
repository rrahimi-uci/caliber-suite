"""``server.py`` wiring tests for the real GitHub Workspace source provider (`P4-E`).

Proves the feature flag actually gates registration -- the registry stays
empty (the pre-`P4-E` default, safe for every deployment that has not
opted in) unless ``github_source_control_enabled`` is set *and* an
encrypted secret store is configured, and that turning both on registers a
real, credential-capable ``GitHubWorkspaceSourceProvider`` rather than a
placeholder.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette

from caliber.config import CaliberConfig
from caliber.github_workspace_provider import GitHubWorkspaceSourceProvider
from caliber.server import create_app


def _build_app(config: CaliberConfig) -> Starlette:
    app = create_app(config=config)
    return app


def _dispose(app: Starlette) -> None:
    app.state.engine.dispose()


def test_github_provider_is_not_registered_by_default(app_config: CaliberConfig) -> None:
    assert app_config.github_source_control_enabled is False
    app = _build_app(app_config)
    try:
        assert app.state.workspace_source_registry.get("github") is None
    finally:
        _dispose(app)


def test_flag_enabled_without_a_secret_store_still_leaves_the_registry_empty(
    app_config: CaliberConfig,
) -> None:
    """No secret store means nowhere for connection credentials to live.

    Registering a provider anyway would be worse than not registering one:
    every call would fail at credential-resolution time instead of the
    registry cleanly reporting "no adapter installed" up front.
    """
    config = app_config.model_copy(update={"github_source_control_enabled": True})
    app = _build_app(config)
    try:
        assert app.state.secret_store is None
        assert app.state.workspace_source_registry.get("github") is None
    finally:
        _dispose(app)


def test_flag_enabled_with_a_secret_store_registers_a_real_provider(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.secret_store import generate_key

    monkeypatch.setenv("CALIBER_TEST_GITHUB_SOURCE_KEY", generate_key())
    config = app_config.model_copy(
        update={
            "github_source_control_enabled": True,
            "secret_encryption_key_source": "CALIBER_TEST_GITHUB_SOURCE_KEY",
        }
    )
    app = _build_app(config)
    try:
        assert app.state.secret_store is not None
        provider = app.state.workspace_source_registry.get("github")
        assert isinstance(provider, GitHubWorkspaceSourceProvider)
        assert provider.name == "github"
    finally:
        _dispose(app)
