"""Evaluation provider Protocol and shared types.

The Eval stage of the refinement pipeline runs the candidate against an
eval dataset, scores it, and compares to the baseline (the currently
deployed artifact). This module declares the contract between the
orchestrator and whatever runs that evaluation.

Two implementations ship:

* :class:`FakeEvalProvider` — deterministic stub used by every unit test.
* :class:`MLflowEvalProvider` (in ``mlflow_runner.py``) — production wrapper
  around ``mlflow.genai.evaluate`` with the standard scorer suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ScoreSet:
    """Scores from a single evaluation pass.

    ``overall`` is the aggregate score the gate compares against the
    ``min_aggregate_score`` threshold. ``dimensions`` is the per-scorer
    breakdown the UI renders in the side-by-side bars on the approval
    detail page (Factual / Tool Use / Tone / Safety in the demo story).
    """

    overall: float
    dimensions: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalComparison:
    """Candidate vs. baseline result, ready for the regression gate.

    ``baseline`` is ``None`` on cold-start runs (no deployed prompt to
    compare against). The gate accepts the cold-start path as long as the
    ``min_aggregate_score`` threshold is met.

    ``deltas`` is computed by the provider so consumers don't have to
    re-subtract — it's also what the audit row records and the UI
    renders as the "+6.1pp" chips.
    """

    candidate: ScoreSet
    baseline: ScoreSet | None
    deltas: dict[str, float] = field(default_factory=dict)
    eval_dataset_id: str = ""
    n_examples: int = 0


@dataclass(frozen=True)
class EvalRequest:
    """Input to the evaluation provider.

    ``baseline_content`` is ``None`` for cold-start runs. The provider
    handles that by returning a comparison with ``baseline=None``.
    """

    agent_id: str
    job_id: str
    artifact_type: str
    candidate_content: str
    baseline_content: str | None
    eval_dataset_id: str
    # Pinned eval-dataset version for reproducible prompt-optimization runs.
    # ``None`` means "use the dataset's current active set" (workflow
    # calibration and unpinned legacy jobs); an int resolves the example set
    # exactly as the dataset contained it at that version.
    eval_dataset_version: int | None = None
    scorer_names: list[str] = field(default_factory=list)
    scorer_configs: dict[str, dict[str, object]] = field(default_factory=dict)
    scorer_weights: dict[str, float] = field(default_factory=dict)


class EvalProviderError(Exception):
    """Raised when the eval pipeline fails (dataset missing, scorer crash, etc.).

    The orchestrator catches this and marks the job ``failed`` with the
    error in ``error_message``. Same contract as ``LLMProviderError``.
    """


class EvalProvider(Protocol):
    """The eval surface CALIBER actually depends on.

    Implementations: read the eval dataset, run candidate + baseline
    through the agent's scorers, return :class:`EvalComparison`. Always
    raise :class:`EvalProviderError` on failure — never the backend's
    raw exception type — so the worker's error handling stays simple.
    """

    def evaluate(self, request: EvalRequest) -> EvalComparison: ...


def apply_scorer_weights(scores: ScoreSet, scorer_weights: dict[str, float]) -> ScoreSet:
    """Return ``scores`` with ``overall`` recomputed from requested weights.

    Providers still own the per-scorer dimensions. This helper only applies a
    manual prompt-optimization weighting contract when the requested scorer
    names are present in the provider output.
    """
    weighted_sum = 0.0
    total_weight = 0.0
    for scorer_name, raw_weight in scorer_weights.items():
        if not isinstance(raw_weight, (int, float)) or isinstance(raw_weight, bool):
            continue
        if raw_weight <= 0:
            continue
        score = scores.dimensions.get(scorer_name)
        if score is None:
            continue
        weighted_sum += float(score) * float(raw_weight)
        total_weight += float(raw_weight)

    if total_weight <= 0:
        return scores

    return ScoreSet(
        overall=round(weighted_sum / total_weight, 4),
        dimensions=dict(scores.dimensions),
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_provider(
    provider: str,
    *,
    session_factory: Any | None = None,
    config: Any | None = None,
) -> EvalProvider:
    """Pick an :class:`EvalProvider` implementation by name.

    Mirrors :func:`caliber.llm.provider.build_provider` and
    :func:`caliber.artifact_store.build_store`.

    For the ``mlflow`` provider, ``session_factory`` wires a DB-backed dataset
    loader (CALIBER eval datasets live in Postgres) and ``config`` wires a
    real LLM-backed default predict_fn, so the eval gate runs without per-agent
    registration. Both are optional — omitting them preserves the strict
    register-first behaviour and never fabricates scores.
    """
    provider_norm = provider.lower()
    if provider_norm == "fake":
        # Lazy import: caliber.eval.fake imports from this module.
        from caliber.eval.fake import FakeEvalProvider  # noqa: PLC0415

        return FakeEvalProvider()
    if provider_norm == "mlflow":
        # Lazy import: production impl pulls in mlflow.genai bits at
        # import time, and tests should not need it.
        from caliber.eval.mlflow_runner import MLflowEvalProvider  # noqa: PLC0415
        from caliber.eval.predict import (  # noqa: PLC0415
            build_completion_fn,
            build_db_load_dataset,
            build_default_predict_fn_factory,
        )

        default_factory = None
        if config is not None:
            complete = build_completion_fn(config)
            if complete is not None:
                default_factory = build_default_predict_fn_factory(complete)
        load_dataset = (
            build_db_load_dataset(session_factory) if session_factory is not None else None
        )
        return MLflowEvalProvider(
            load_dataset=load_dataset,
            default_factory=default_factory,
        )
    raise EvalProviderError(f"unknown eval_provider {provider!r}")
