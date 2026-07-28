"""Deploy-gate threshold evaluation — the part that turns a replay into evidence.

The review's finding was precise: the gate *ran* the workflow but measured only
successful completion, ignored two threshold fields the Inspector exposes, and
therefore could not authorize a release. Two distinct defects hide in that:

1. **Completion is not quality.** A workflow that returns the wrong answer
   completes. The gate now scores each replay against the dataset's expected
   output using the same deterministic scorers the evaluation product already
   ships (:mod:`caliber.eval.scorecard`), and additionally measures latency and
   token cost, so a release can be gated on quality and spend rather than on
   "it did not crash".

2. **A silently ignored threshold is worse than a missing one.** It reads as a
   configured safety control while enforcing nothing. Every threshold key is now
   either evaluated here or rejected as unsupported, and an unsupported key fails
   the gate **closed**. That property is what makes the vocabulary trustworthy as
   it grows: a threshold cannot be added to the UI and quietly do nothing.

This module owns only the verdict — sampling, replay, and Preview containment
stay in :mod:`caliber.workflows.promoter`. Keeping them apart is what lets the
threshold rules be unit-tested without a workflow runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Threshold keys this module understands, mapped to a one-line meaning. Used
#: both to evaluate and to reject unknown keys, so the two can never drift.
SUPPORTED_THRESHOLDS: dict[str, str] = {
    "min_pass_rate": "Weighted fraction of replays that completed AND met the scorer pass threshold.",
    "min_completion_rate": "Fraction of replays that reached a completed status.",
    "min_overall": "Weighted mean of every configured scorer across the sample.",
    "min_exact_match": "Weighted mean of the exact_match scorer.",
    "min_token_f1": "Weighted mean of the token_f1 scorer.",
    "min_contains_expected": "Weighted mean of the contains_expected scorer.",
    "min_non_empty": "Weighted mean of the non_empty scorer.",
    "min_overall_delta": (
        "Candidate overall minus the alias's currently deployed version on the same "
        "sample. Requires a current deployment to compare against."
    ),
    "max_avg_latency_ms": "Mean wall-clock duration of a replay.",
    "max_p95_latency_ms": "95th-percentile wall-clock duration of a replay.",
    "max_avg_tokens": "Mean tokens consumed per replay.",
    "max_total_tokens": "Total tokens consumed across the sample.",
    "max_error_rate": "Fraction of replays that failed to complete or errored while scoring.",
}

#: ``min_*`` keys are lower bounds; ``max_*`` keys are upper bounds. Derived from
#: the name so a new threshold cannot be added with the comparison inverted.
_LOWER_BOUND_PREFIX = "min_"
_UPPER_BOUND_PREFIX = "max_"

#: Which metric a threshold reads. A threshold whose metric is unavailable for
#: this sample fails closed rather than being skipped.
_THRESHOLD_METRIC: dict[str, str] = {
    "min_pass_rate": "pass_rate",
    "min_completion_rate": "completion_rate",
    "min_overall": "overall",
    "min_exact_match": "scorer.exact_match",
    "min_token_f1": "scorer.token_f1",
    "min_contains_expected": "scorer.contains_expected",
    "min_non_empty": "scorer.non_empty",
    "min_overall_delta": "overall_delta",
    "max_avg_latency_ms": "avg_latency_ms",
    "max_p95_latency_ms": "p95_latency_ms",
    "max_avg_tokens": "avg_tokens",
    "max_total_tokens": "total_tokens",
    "max_error_rate": "error_rate",
}


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of ``values``.

    Nearest-rank rather than interpolated: with the small bounded samples a
    deploy gate replays (tens of examples), an interpolated p95 invents a value
    no run actually produced, which is the wrong thing to gate a release on.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = math.ceil(fraction * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]


@dataclass(frozen=True)
class ThresholdOutcome:
    """One threshold's verdict, kept so the gate result can explain itself."""

    key: str
    threshold: float
    metric: str
    value: float | None
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "threshold": self.threshold,
            "metric": self.metric,
            "value": self.value,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class GateMetrics:
    """Everything measured while replaying one gate's sample."""

    n_examples: int = 0
    completed: int = 0
    errored: int = 0
    pass_rate: float = 0.0
    overall: float = 0.0
    scorer_means: dict[str, float] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    #: Examples in the sample that actually carry an expected output. Zero means
    #: quality is **unmeasurable**, not zero — grading a prediction against an
    #: absent expectation produces a number with no meaning, and reporting it as
    #: 0.0 would read as "the workflow answered badly".
    scored_examples: int = 0
    #: Overall of the alias's currently deployed version on the same sample, or
    #: ``None`` when there is nothing deployed to compare against.
    baseline_overall: float | None = None

    @property
    def quality_measurable(self) -> bool:
        return self.scored_examples > 0

    @property
    def completion_rate(self) -> float:
        return self.completed / self.n_examples if self.n_examples else 0.0

    @property
    def error_rate(self) -> float:
        return self.errored / self.n_examples if self.n_examples else 0.0

    def as_metric_map(self) -> dict[str, float | None]:
        """Flatten to the names :data:`_THRESHOLD_METRIC` refers to.

        ``None`` means *not measurable for this sample*, which a threshold reading
        it must treat as a failure, not as a pass.
        """
        values: dict[str, float | None] = {
            "pass_rate": self.pass_rate if self.quality_measurable else None,
            "completion_rate": self.completion_rate,
            "overall": self.overall if self.quality_measurable else None,
            "error_rate": self.error_rate,
            "avg_latency_ms": (
                sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else None
            ),
            "p95_latency_ms": percentile(self.latencies_ms, 0.95) if self.latencies_ms else None,
            "avg_tokens": (sum(self.tokens) / len(self.tokens) if self.tokens else None),
            "total_tokens": float(sum(self.tokens)) if self.tokens else None,
            "overall_delta": (
                self.overall - self.baseline_overall
                if self.baseline_overall is not None and self.quality_measurable
                else None
            ),
        }
        if self.quality_measurable:
            for name, mean in self.scorer_means.items():
                values[f"scorer.{name}"] = mean
        return values

    def to_dict(self) -> dict[str, Any]:
        measured = self.as_metric_map()
        return {
            "n_examples": self.n_examples,
            "completed": self.completed,
            "errored": self.errored,
            "scored_examples": self.scored_examples,
            **{key: value for key, value in measured.items() if value is not None},
            "baseline_overall": self.baseline_overall,
        }


def evaluate_thresholds(
    thresholds: dict[str, float], metrics: GateMetrics
) -> list[ThresholdOutcome]:
    """Evaluate every configured threshold against ``metrics``.

    Ordering is deterministic (sorted by key) so a gate result is comparable
    across runs and diffable in an audit record.
    """
    outcomes: list[ThresholdOutcome] = []
    measured = metrics.as_metric_map()
    if not thresholds:
        return [
            ThresholdOutcome(
                key="",
                threshold=math.nan,
                metric="",
                value=None,
                passed=False,
                detail=(
                    "deploy gate configures no thresholds, so it asserts nothing and "
                    "cannot authorize a promotion; add at least one of: "
                    + ", ".join(sorted(SUPPORTED_THRESHOLDS))
                ),
            )
        ]
    for key in sorted(thresholds):
        raw = thresholds[key]
        try:
            threshold = float(raw)
        except (TypeError, ValueError):
            outcomes.append(
                ThresholdOutcome(
                    key=key,
                    threshold=math.nan,
                    metric="",
                    value=None,
                    passed=False,
                    detail=f"threshold {key!r} is not a number ({raw!r}); gate failed closed",
                )
            )
            continue
        if key not in _THRESHOLD_METRIC:
            outcomes.append(
                ThresholdOutcome(
                    key=key,
                    threshold=threshold,
                    metric="",
                    value=None,
                    passed=False,
                    detail=(
                        f"threshold {key!r} is not supported by the deploy gate; "
                        "remove it or use one of: " + ", ".join(sorted(SUPPORTED_THRESHOLDS))
                    ),
                )
            )
            continue
        metric = _THRESHOLD_METRIC[key]
        value = measured.get(metric)
        if value is None:
            outcomes.append(
                ThresholdOutcome(
                    key=key,
                    threshold=threshold,
                    metric=metric,
                    value=None,
                    passed=False,
                    detail=_unmeasurable_detail(key, metric, metrics),
                )
            )
            continue
        if key.startswith(_LOWER_BOUND_PREFIX):
            passed = value >= threshold
            comparison = f"{value:.4g} >= {threshold:.4g}"
        elif key.startswith(_UPPER_BOUND_PREFIX):
            passed = value <= threshold
            comparison = f"{value:.4g} <= {threshold:.4g}"
        else:  # pragma: no cover - guarded by the SUPPORTED_THRESHOLDS contract
            passed = False
            comparison = f"{key!r} has no min_/max_ bound direction"
        outcomes.append(
            ThresholdOutcome(
                key=key,
                threshold=threshold,
                metric=metric,
                value=value,
                passed=passed,
                detail=f"{metric} {comparison}",
            )
        )
    return outcomes


def _unmeasurable_detail(key: str, metric: str, metrics: GateMetrics) -> str:
    if not metrics.quality_measurable and metric in _QUALITY_METRICS:
        return (
            f"{key} needs graded output, but none of the {metrics.n_examples} sampled "
            "examples carries an expected output; add expected outputs to the dataset "
            "or assert completion with min_completion_rate"
        )
    if key == "min_overall_delta":
        return (
            "min_overall_delta needs a currently deployed version on this alias to "
            "compare against; nothing usable is deployed, so no regression evidence exists"
        )
    if metric.startswith("scorer."):
        name = metric.split(".", 1)[1]
        return (
            f"scorer {name!r} is not configured on this gate, so {key} cannot be "
            "measured; add it to the gate's scorers"
        )
    return f"{metric} could not be measured for this sample, so {key} fails closed"


#: Metrics that require a graded expected output to mean anything.
_QUALITY_METRICS = frozenset({"pass_rate", "overall", "overall_delta"})


__all__ = [
    "SUPPORTED_THRESHOLDS",
    "GateMetrics",
    "ThresholdOutcome",
    "evaluate_thresholds",
    "percentile",
]
