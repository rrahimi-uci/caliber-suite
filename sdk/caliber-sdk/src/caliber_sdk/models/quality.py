"""Typed models for the quality surfaces: datasets, judges, evaluations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalDataset:
    """A versioned evaluation dataset.

    The ``mlflow_*`` fields record the last sync to MLflow's dataset registry.
    They are reported rather than owned: the dataset lives here, and the sync
    is a separate, possibly stale, fact.
    """

    dataset_id: str = ""
    name: str = ""
    description: str | None = None
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str = ""
    version: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    mlflow_dataset_id: str | None = None
    mlflow_synced_at: str | None = None
    mlflow_synced_version: int | None = None
    mlflow_record_count: int | None = None
    mlflow_digest: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_synced(self) -> bool:
        """Whether the dataset has ever been pushed to MLflow.

        Not whether it is *currently* in sync — ``mlflow_synced_version`` can
        lag ``version``, and conflating the two would let a caller trust stale
        evidence.
        """
        return self.mlflow_synced_at is not None


@dataclass
class EvalExample:
    """One row of a dataset."""

    example_id: str = ""
    dataset_id: str = ""
    inputs: Any = None
    expected: Any = None
    example_metadata: dict[str, Any] = field(default_factory=dict)
    source_trace_id: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Judge:
    """A model-backed grader.

    ``feedback_value_type`` is what a scorecard reads: a bool judge and a
    numeric one are not interchangeable, and the field is how a caller knows
    which they have.
    """

    judge_id: str = ""
    name: str = ""
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    feedback_value_type: str | None = None
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Evaluation:
    """One scored run over a dataset.

    ``metrics`` and ``results`` stay open: which scorers ran is a property of
    the evaluation, not of this type, and enumerating them here would go stale
    the first time a scorer is added.
    """

    evaluation_id: str = ""
    dataset_id: str | None = None
    name: str | None = None
    status: str = ""
    target_type: str | None = None
    target_ref: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    results: Any = None
    created_by: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in {"succeeded", "completed", "failed", "error", "cancelled"}


@dataclass
class JudgeAlignment:
    """Agreement between a judge and human reviewers.

    Cohen's kappa matters more than raw agreement: a judge that always says
    "pass" agrees with a mostly-passing sample while measuring nothing.
    """

    judge_id: str = ""
    agreement: float | None = None
    kappa: float | None = None
    sample_size: int = 0
    per_example: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["EvalDataset", "EvalExample", "Evaluation", "Judge", "JudgeAlignment"]
