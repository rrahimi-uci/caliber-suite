"""The real ``mlflow`` promoter and artifact store are the defaults (alpha gap P0-3).

A ``fake`` promoter returns ``applied`` and writes ``promote`` audit rows while the
MLflow registry is unchanged, so on a first install that follows the documented steps
Apply used to report a release that never happened. The defaults are now the real
providers, and choosing a fake one needs an explicit opt-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.promoter import CompositePromoter, MLflowPromoter
from caliber.server import create_app


def _config(tmp_path: Path, **env: str) -> CaliberConfig:
    base = {
        "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'guard.db'}",
        "CALIBER_AUTH_MODE": "trusted_header",
        "CALIBER_ADMIN_USERS": "@test",
        "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        "CALIBER_ASSISTANT_ENGINE": "fake",
        "CALIBER_WORKFLOW_STORAGE_BASE_URI": f"file://{tmp_path / 'workspaces'}",
    }
    base.update(env)
    return CaliberConfig.load(environ=base)


def test_the_shipped_defaults_are_the_real_providers() -> None:
    fields = CaliberConfig.model_fields
    assert fields["promoter_provider"].default == "mlflow"
    assert fields["artifact_store_provider"].default == "mlflow"
    assert fields["allow_simulated_providers"].default is False


def test_the_opt_in_is_read_from_the_environment(tmp_path: Path) -> None:
    assert _config(tmp_path, CALIBER_ALLOW_SIMULATED_PROVIDERS="true").allow_simulated_providers
    assert not _config(
        tmp_path, CALIBER_ALLOW_SIMULATED_PROVIDERS="false"
    ).allow_simulated_providers


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"CALIBER_PROMOTER_PROVIDER": "fake"}, ["CALIBER_PROMOTER_PROVIDER=fake"]),
        (
            {"CALIBER_ARTIFACT_STORE_PROVIDER": "fake"},
            ["CALIBER_ARTIFACT_STORE_PROVIDER=fake"],
        ),
        (
            {"CALIBER_PROMOTER_PROVIDER": "FAKE", "CALIBER_ARTIFACT_STORE_PROVIDER": " fake "},
            ["CALIBER_PROMOTER_PROVIDER=fake", "CALIBER_ARTIFACT_STORE_PROVIDER=fake"],
        ),
    ],
    ids=["promoter", "artifact-store", "both-any-case"],
)
def test_a_fake_provider_is_refused_without_the_opt_in(
    tmp_path: Path, env: dict[str, str], named: list[str]
) -> None:
    overrides = {
        "CALIBER_PROMOTER_PROVIDER": "mlflow",
        "CALIBER_ARTIFACT_STORE_PROVIDER": "mlflow",
        "CALIBER_ALLOW_SIMULATED_PROVIDERS": "false",
        **env,
    }
    config = _config(tmp_path, **overrides)
    with pytest.raises(ValueError, match="simulate releases") as excinfo:
        create_app(config=config)
    message = str(excinfo.value)
    for fragment in named:
        assert fragment in message
    assert "CALIBER_ALLOW_SIMULATED_PROVIDERS=true" in message


def test_a_fake_provider_boots_once_opted_in(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        CALIBER_PROMOTER_PROVIDER="fake",
        CALIBER_ARTIFACT_STORE_PROVIDER="fake",
        CALIBER_ALLOW_SIMULATED_PROVIDERS="true",
    )
    app = create_app(config=config)
    try:
        assert getattr(app.state.promoter, "is_simulated", False) is True
    finally:
        app.state.engine.dispose()


def test_the_default_app_uses_the_real_promoter_and_reports_no_simulated_release(
    tmp_path: Path,
) -> None:
    """With no provider variables set at all, nothing about the release path is
    simulated: the readiness banner lists only the LLM and evaluation providers."""
    config = _config(
        tmp_path,
        CALIBER_PROMOTER_PROVIDER="mlflow",
        CALIBER_ARTIFACT_STORE_PROVIDER="mlflow",
        CALIBER_ALLOW_SIMULATED_PROVIDERS="false",
    )
    app = create_app(config=config)
    try:
        promoter = app.state.promoter
        assert isinstance(promoter, CompositePromoter)
        assert isinstance(promoter._default, MLflowPromoter)
        assert getattr(promoter, "is_simulated", False) is False
        with TestClient(app, headers={"X-CALIBER-User": "@test"}) as client:
            data = client.get("/ajax-api/2.0/mlflow/caliber/readiness").json()["data"]
        assert "promoter" not in data["simulated"]
        assert "artifact_store" not in data["simulated"]
    finally:
        app.state.engine.dispose()


# ---------------------------------------------------------------------------
# A real LLM scored by the fake evaluator (alpha gap P0-4)
# ---------------------------------------------------------------------------


def _unit_config(tmp_path: Path, **env: str) -> CaliberConfig:
    overrides = {
        "CALIBER_PROMOTER_PROVIDER": "mlflow",
        "CALIBER_ARTIFACT_STORE_PROVIDER": "mlflow",
        "CALIBER_ALLOW_SIMULATED_PROVIDERS": "false",
        **env,
    }
    return _config(tmp_path, **overrides)


def test_a_real_llm_with_the_fake_evaluator_is_refused_without_the_opt_in(tmp_path: Path) -> None:
    from caliber.server import _require_simulated_opt_in

    config = _unit_config(tmp_path, CALIBER_LLM_PROVIDER="openai", CALIBER_EVAL_PROVIDER="fake")
    with pytest.raises(ValueError, match="canned numbers") as excinfo:
        _require_simulated_opt_in(config)
    assert "CALIBER_EVAL_PROVIDER=mlflow" in str(excinfo.value)
    assert "CALIBER_ALLOW_SIMULATED_PROVIDERS=true" in str(excinfo.value)


@pytest.mark.parametrize(
    "env",
    [
        {"CALIBER_LLM_PROVIDER": "fake", "CALIBER_EVAL_PROVIDER": "fake"},  # the offline default
        {"CALIBER_LLM_PROVIDER": "openai", "CALIBER_EVAL_PROVIDER": "mlflow"},  # real evidence
        {"CALIBER_LLM_PROVIDER": "fake", "CALIBER_EVAL_PROVIDER": "mlflow"},
        {
            "CALIBER_LLM_PROVIDER": "openai",
            "CALIBER_EVAL_PROVIDER": "fake",
            "CALIBER_ALLOW_SIMULATED_PROVIDERS": "true",
        },  # deliberate
    ],
    ids=["offline-default", "real-both", "fake-llm-real-eval", "opted-in"],
)
def test_the_allowed_llm_and_evaluator_combinations_boot(
    tmp_path: Path, env: dict[str, str]
) -> None:
    from caliber.server import _require_simulated_opt_in

    _require_simulated_opt_in(_unit_config(tmp_path, **env))
