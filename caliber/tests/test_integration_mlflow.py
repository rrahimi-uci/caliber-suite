"""Integration tests against a real MLflow tracking + prompt registry.

These tests stand the integrations up end-to-end without mocking mlflow:

* :class:`MLflowArtifactStore` — register a prompt, then read it back.
* :class:`MLflowPromoter` — register a new version, rotate the alias,
  verify the alias points at the new version.
* :class:`MLflowEvalProvider` — register a predict_fn factory, call
  ``mlflow.genai.evaluate`` with a small synthetic dataset, verify the
  comparison contains the expected dimensions.

The whole module is **opt-in** — disabled unless ``CALIBER_INTEGRATION_TESTS=1``
is exported. Real MLflow imports + tracking-store setup are slow and
require ``mlflow`` to be installed, so CI runs them in a separate job
that only triggers when the contributor explicitly opts in.

Each test gets a fresh SQLite-backed tracking store under ``tmp_path``,
so tests are isolated and don't depend on a remote MLflow server.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from caliber.artifact_store import MLflowArtifactStore
from caliber.eval.mlflow_runner import MLflowEvalProvider
from caliber.eval.provider import EvalProviderError, EvalRequest
from caliber.promoter import MLflowPromoter, PromotionRequest

# Module-level opt-in gate: skip every test in this file unless the
# environment explicitly asks for integration tests.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CALIBER_INTEGRATION_TESTS") != "1",
        reason="set CALIBER_INTEGRATION_TESTS=1 to run integration tests",
    ),
]

# Skip the whole module cleanly if mlflow isn't installed — important when
# the suite runs in environments that don't pull the full ``mlflow`` extra.
mlflow = pytest.importorskip("mlflow", reason="mlflow not installed")


def _dispose_mlflow_migration_registries() -> None:
    """Drop stray MLflow migration mappers that leak across tests.

    MLflow's SQLite bootstrap imports historical Alembic revision modules
    that define temporary ``SqlRun`` / ``SqlTag`` declarative classes. In a
    long-lived pytest process, those registries can survive after the
    migration step and later confuse ``sqlalchemy.orm.configure_mappers()``
    when the prompt registry initializes.

    The registries are test scaffolding only, so disposing them between
    integration tests keeps the real MLflow tracking/prompt models isolated.
    """

    mapper_mod = importlib.import_module("sqlalchemy.orm.mapper")
    for registry in list(mapper_mod._all_registries()):
        modules = {
            value.__module__
            for value in registry._class_registry.values()
            if isinstance(value, type)
        }
        if any(
            module.endswith("_migrate_user_column_to_tags_py")
            or module.startswith("mlflow.store.db_migrations.versions.")
            for module in modules
        ):
            registry.dispose()


@pytest.fixture(autouse=True)
def isolate_mlflow_migration_state() -> Iterator[None]:
    _dispose_mlflow_migration_registries()
    try:
        yield
    finally:
        _dispose_mlflow_migration_registries()


@pytest.fixture
def mlflow_tracking_uri(tmp_path: Path) -> Iterator[str]:
    """Point MLflow at a fresh SQLite-backed tracking store under ``tmp_path``.

    Each test gets its own store so prompts and aliases registered by one
    test don't leak into the next. The previous tracking URI is restored
    on teardown so we don't poison the rest of the suite.
    """
    db_path = tmp_path / "mlflow.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    uri = f"sqlite:///{db_path}"

    previous_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(uri)
    # Some MLflow versions also need a separate registry URI; default it
    # to the tracking URI so the prompt registry shares the same store.
    mlflow.set_registry_uri(uri)
    try:
        yield uri
    finally:
        mlflow.set_tracking_uri(previous_uri)
        mlflow.set_registry_uri(previous_uri)


# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


def test_mlflow_artifact_store_reads_aliased_prompt(mlflow_tracking_uri: str) -> None:
    _ = mlflow_tracking_uri
    agent_id = "support-agent"
    content = "You are a helpful agent."

    # Use ``mlflow.genai`` — the top-level aliases emit ``FutureWarning``
    # on MLflow 3.13+.
    version = mlflow.genai.register_prompt(name=agent_id, template=content)
    mlflow.genai.set_prompt_alias(name=agent_id, alias="prod", version=version.version)

    store = MLflowArtifactStore(alias="prod")
    assert store.get_active_prompt(agent_id) == content


def test_mlflow_artifact_store_returns_none_for_missing_prompt(
    mlflow_tracking_uri: str,
) -> None:
    _ = mlflow_tracking_uri
    store = MLflowArtifactStore(alias="prod")
    assert store.get_active_prompt("never-registered") is None


# ---------------------------------------------------------------------------
# Promoter
# ---------------------------------------------------------------------------


def test_mlflow_promoter_registers_and_rotates_alias(mlflow_tracking_uri: str) -> None:
    _ = mlflow_tracking_uri
    agent_id = "support-agent"
    # Seed an initial version so the promoted one is v2, exercising
    # the alias-rotation path (not just first-time registration).
    v1 = mlflow.genai.register_prompt(name=agent_id, template="v1 prompt")
    mlflow.genai.set_prompt_alias(name=agent_id, alias="prod", version=v1.version)

    promoter = MLflowPromoter(alias="prod")
    result = promoter.promote(
        PromotionRequest(
            agent_id=agent_id,
            artifact_type="prompt",
            new_content="v2 prompt — improved",
            rationale="add reasoning rubric",
            approval_id="AP-1",
        )
    )

    # New version registered.
    assert result.details["version"] == v1.version + 1
    assert result.details["alias"] == "prod"

    # Alias now points at the new content. The URI must use the
    # ``prompts:/<name>@<alias>`` form — a bare ``<name>@<alias>`` is
    # not parsed as an alias ref by MLflow 3.13+.
    aliased = mlflow.genai.load_prompt(f"prompts:/{agent_id}@prod")
    assert aliased.template == "v2 prompt — improved"


def test_mlflow_promoter_first_time_registration(mlflow_tracking_uri: str) -> None:
    """Cold-start path: no prior version, no prior alias. The promoter
    should still register v1 and create the alias from scratch."""
    _ = mlflow_tracking_uri
    promoter = MLflowPromoter(alias="prod")
    result = promoter.promote(
        PromotionRequest(
            agent_id="fresh-agent",
            artifact_type="prompt",
            new_content="initial prompt",
            rationale="",
            approval_id="AP-1",
        )
    )
    assert result.details["version"] == 1
    aliased = mlflow.genai.load_prompt("prompts:/fresh-agent@prod")
    assert aliased.template == "initial prompt"


# ---------------------------------------------------------------------------
# EvalProvider
# ---------------------------------------------------------------------------


def test_mlflow_eval_provider_runs_evaluate_end_to_end(mlflow_tracking_uri: str) -> None:
    """Run ``mlflow.genai.evaluate`` with a tiny synthetic dataset.

    The predict_fn echoes the prompt prefix so the scorer (a custom
    pass/fail callable) can tell candidate from baseline output. We use
    a custom scorer rather than the LLM-judge defaults so the test is
    deterministic and offline.
    """
    _ = mlflow_tracking_uri

    # A predict_fn that returns the prompt prefix appended to the input.
    # ``mlflow.genai.evaluate`` passes each example's ``inputs`` dict as
    # keyword arguments to predict_fn (one kwarg per key in the dict).
    def make_predict_fn(prompt: str) -> Any:
        def predict(question: str = "") -> str:
            return f"{prompt}: {question}"

        return predict

    # A deterministic scorer — passes when the output starts with the
    # candidate prompt prefix. Using ``mlflow.genai.scorer`` decorator if
    # available; otherwise fall back to a duck-typed scorer dict.
    try:
        from mlflow.genai import scorer  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        pytest.skip("mlflow.genai.scorer decorator not available in this version")

    @scorer  # type: ignore[misc]
    def starts_with_new(outputs: str) -> float:
        return 1.0 if outputs.startswith("new") else 0.0

    scorers = [starts_with_new]

    # ``mlflow.genai.evaluate`` requires each example to have an
    # ``inputs`` key (dict of named arguments to predict_fn).
    data = [
        {"inputs": {"question": "what is x?"}},
        {"inputs": {"question": "what is y?"}},
    ]

    provider = MLflowEvalProvider(
        load_dataset=lambda _id: data,
        scorers=scorers,
    )
    provider.register_predict_fn("support-agent", make_predict_fn)

    comparison = provider.evaluate(
        EvalRequest(
            agent_id="support-agent",
            job_id="RFN-1",
            artifact_type="prompt",
            candidate_content="new",
            baseline_content="old",
            eval_dataset_id="default",
        )
    )

    assert comparison.candidate.overall > 0.9  # candidate always starts with "new"
    assert comparison.baseline is not None
    assert comparison.baseline.overall < 0.1  # baseline starts with "old", never "new"
    assert comparison.deltas["overall"] > 0.8
    assert comparison.n_examples == 2


def test_mlflow_eval_provider_raises_without_registered_factory(
    mlflow_tracking_uri: str,
) -> None:
    _ = mlflow_tracking_uri
    provider = MLflowEvalProvider(load_dataset=lambda _id: [{"q": "a"}])
    with pytest.raises(EvalProviderError, match=r"no predict_fn .*unknown-agent"):
        provider.evaluate(
            EvalRequest(
                agent_id="unknown-agent",
                job_id="RFN-1",
                artifact_type="prompt",
                candidate_content="x",
                baseline_content=None,
                eval_dataset_id="default",
            )
        )
