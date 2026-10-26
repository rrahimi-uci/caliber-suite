"""Tests for the /readiness provider-honesty endpoint (golden-path Wave 3)."""

from __future__ import annotations

from starlette.testclient import TestClient

from caliber.routes.health import READINESS_PATH


def test_readiness_reports_simulated_providers_by_default(client: TestClient) -> None:
    # The default test config uses the 'fake' providers.
    resp = client.get(READINESS_PATH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["providers"] == {
        "llm": "fake",
        "eval": "fake",
        "promoter": "fake",
        "artifact_store": "fake",
    }
    assert sorted(data["simulated"]) == ["artifact_store", "eval", "llm", "promoter"]
    assert data["all_real"] is False
    # Observability flags are exposed (defaults: tracing on, judge off).
    assert data["tracing_enabled"] is True
    assert data["workflow_llm_judge_enabled"] is False


def test_readiness_leaks_no_secrets(client: TestClient) -> None:
    body = client.get(READINESS_PATH).text.lower()
    # Only enum-y provider values + flags; never keys/secrets.
    for needle in ("api_key", "secret", "openai_api_key", "sk-"):
        assert needle not in body


def test_readiness_reports_profile(client: TestClient) -> None:
    """The profile the process was started under is part of readiness, for both profiles."""
    assert client.get(READINESS_PATH).json()["data"]["profile"] == "development"
    config = client.app.state.config
    client.app.state.config = config.model_copy(update={"profile": "reference"})
    assert client.get(READINESS_PATH).json()["data"]["profile"] == "reference"


def test_a_readiness_report_names_its_profile() -> None:
    from caliber.observability.readiness import ReadinessReport

    assert ReadinessReport(checks=[]).to_dict()["profile"] == "development"
    assert ReadinessReport(checks=[], profile="reference").to_dict()["profile"] == "reference"
