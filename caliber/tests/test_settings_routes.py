"""Tests for the Settings runtime configuration inventory."""

from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

import caliber.routes.settings as settings_route
from caliber.runtime_advisories import RuntimeDependencyAdvisory

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _settings_by_key(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    groups = payload["groups"]
    assert isinstance(groups, list)
    result: dict[str, dict[str, object]] = {}
    for group in groups:
        assert isinstance(group, dict)
        settings = group["settings"]
        assert isinstance(settings, list)
        for setting in settings:
            assert isinstance(setting, dict)
            result[str(setting["key"])] = setting
    return result


def test_llm_setup_status_reports_provider_and_presence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client.app.state.config = client.app.state.config.model_copy(
        update={"llm_provider": "openai", "llm_base_url": "http://gw:5000/v1"}
    )

    resp = client.get(f"{PREFIX}/settings/llm")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["llm_provider"] == "openai"
    assert data["gateway_url"] == "http://gw:5000/v1"
    assert data["openai_key_present"] is True
    assert data["anthropic_key_present"] is False
    # Status returns the resolved key values so the Settings UI can prefill the
    # fields with the live environment defaults.
    assert data["openai_api_key"] == "sk-test"
    assert data["anthropic_api_key"] == ""


def test_llm_setup_applies_keys_and_gateway_at_runtime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    resp = client.patch(
        f"{PREFIX}/settings/llm",
        json={
            "openai_api_key": "sk-new",
            "anthropic_api_key": "sk-ant",
            "gateway_url": "http://gateway.local:5000/v1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["gateway_url"] == "http://gateway.local:5000/v1"
    assert data["openai_key_present"] is True
    assert data["anthropic_key_present"] is True
    # Applied as a runtime override: process env + app.state config.
    assert os.environ["OPENAI_API_KEY"] == "sk-new"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant"
    assert client.app.state.config.llm_base_url == "http://gateway.local:5000/v1"


def test_llm_setup_update_requires_admin(client: TestClient) -> None:
    resp = client.patch(
        f"{PREFIX}/settings/llm",
        json={"gateway_url": "http://x"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


def test_runtime_settings_lists_grouped_config_surface(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "allow_flagged_dspy_optimizers": False,
            "allow_flagged_local_embeddings": False,
        }
    )
    resp = client.get(f"{PREFIX}/settings/runtime")
    assert resp.status_code == 200

    data = resp.json()["data"]
    groups = data["groups"]
    assert [group["id"] for group in groups] == [
        "assistant",
        "model-providers",
        "memory",
        "storage",
        "knowledge",
        "security",
        "runtime-advisories",
        "operations",
        "tool-sandbox",
    ]

    settings = _settings_by_key(data)
    assert settings["assistant_model"]["control"] == "live"
    assert settings["assistant_model"]["restart_required"] is False
    assert settings["openai_workflow_api"]["env_var"] == "CALIBER_OPENAI_WORKFLOW_API"
    assert settings["openai_workflow_api"]["display_value"] == "chat_completions"
    assert (
        settings["openai_workflow_parallel_tool_calls"]["env_var"]
        == "CALIBER_OPENAI_WORKFLOW_PARALLEL_TOOL_CALLS"
    )
    assert settings["openai_workflow_parallel_tool_calls"]["display_value"] == "auto"
    assert settings["openai_prompt_cache_mode"]["env_var"] == "CALIBER_OPENAI_PROMPT_CACHE_MODE"
    assert settings["openai_prompt_cache_mode"]["display_value"] == "auto"
    assert (
        settings["openai_prompt_cache_retention"]["env_var"]
        == "CALIBER_OPENAI_PROMPT_CACHE_RETENTION"
    )
    assert settings["openai_prompt_cache_retention"]["display_value"] == "default"
    assert settings["workflow_storage.backend"]["env_var"] == "CALIBER_WORKFLOW_STORAGE_BACKEND"
    assert settings["knowledge_age_enabled"]["env_var"] == "CALIBER_KNOWLEDGE_AGE_ENABLED"
    assert settings["knowledge_age_enabled"]["display_value"] == "Disabled"
    assert settings["knowledge_age_viewer_url"]["env_var"] == "CALIBER_KNOWLEDGE_AGE_VIEWER_URL"
    assert settings["knowledge_age_viewer_url"]["display_value"] == "Not configured"
    assert (
        settings["allow_flagged_dspy_optimizers"]["env_var"]
        == "CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS"
    )
    assert settings["allow_flagged_dspy_optimizers"]["display_value"] == "Disabled"
    assert settings["knowledge_graph_extractor_backend"]["display_value"] == "heuristic"
    assert (
        settings["allow_flagged_local_embeddings"]["env_var"]
        == "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS"
    )
    assert settings["allow_flagged_local_embeddings"]["display_value"] == "Disabled"
    assert settings["log_bucket"]["display_value"] == "caliber-log"
    assert settings["log_sink"]["env_var"] == "CALIBER_LOG_SINK"
    assert settings["database_url"]["sensitive"] is True
    assert data["summary"]["total"] == len(settings)


def test_runtime_settings_masks_database_password(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"database_url": "postgresql://caliber:super-secret@db.example/caliber"}
    )

    resp = client.get(f"{PREFIX}/settings/runtime")
    assert resp.status_code == 200
    settings = _settings_by_key(resp.json()["data"])

    database = settings["database_url"]
    assert database["display_value"] == "postgresql://caliber:********@db.example/caliber"
    assert "super-secret" not in database["display_value"]


def test_runtime_settings_surfaces_dependency_advisories(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings_route,
        "get_runtime_dependency_advisories",
        lambda: [
            RuntimeDependencyAdvisory(
                package_name="diskcache",
                installed_version="5.6.3",
                advisory_ids=("CVE-2025-69872",),
                summary=(
                    "diskcache 5.6.3 is flagged and is pulled in by the optional "
                    "DSPy optimizer stack."
                ),
                recommended_action=(
                    "Avoid enabling DSPy-backed refinement flows in sensitive deployments until "
                    "upstream publishes a fixed release."
                ),
            ),
            RuntimeDependencyAdvisory(
                package_name="litellm",
                installed_version="1.83.0",
                advisory_ids=("CVE-2026-42203", "CVE-2026-42208"),
                summary=(
                    "litellm 1.83.0 is flagged and is pulled in by the optional "
                    "DSPy optimizer stack."
                ),
                recommended_action=(
                    "Upgrade the optional DSPy / LiteLLM stack in a supported Python runtime "
                    "before re-enabling optimizer-backed refinement flows."
                ),
            ),
        ],
    )

    resp = client.get(f"{PREFIX}/settings/runtime")
    assert resp.status_code == 200

    data = resp.json()["data"]
    groups = data["groups"]
    advisory_group = next(group for group in groups if group["id"] == "runtime-advisories")
    assert advisory_group["configured_count"] == 2
    assert advisory_group["live_editable_count"] == 0

    settings = _settings_by_key(data)
    advisory = settings["dependency_advisory.diskcache"]
    assert advisory["display_value"] == "5.6.3 (CVE-2025-69872)"
    assert "optional DSPy optimizer stack" in advisory["description"]
    assert advisory["env_var"] == "runtime://dependency-advisories/diskcache"
    litellm = settings["dependency_advisory.litellm"]
    assert litellm["display_value"] == "1.83.0 (CVE-2026-42203, CVE-2026-42208)"
    assert "optional DSPy optimizer stack" in litellm["description"]
    assert litellm["env_var"] == "runtime://dependency-advisories/litellm"
