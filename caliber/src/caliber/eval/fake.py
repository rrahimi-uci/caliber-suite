"""Deterministic eval provider for tests.

The fake builds a plausible :class:`EvalComparison` from canned defaults
that tests can override. The default behavior is "candidate slightly
better than baseline on every dimension" — enough to clear a default
regression gate — so a worker end-to-end test passes without bespoke
fixture setup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from caliber.eval.provider import EvalComparison, EvalRequest, ScoreSet, apply_scorer_weights


@dataclass
class FakeEvalProvider:
    """In-memory eval double.

    Attributes
    ----------
    candidate_scores:
        Override the candidate :class:`ScoreSet` returned by every call.
    baseline_scores:
        Override the baseline :class:`ScoreSet`. Set to ``None`` to
        simulate a cold-start run.
    n_examples:
        Eval-set size reported in the comparison. Inspected by the UI.
    eval_callable:
        Optional callable that takes an :class:`EvalRequest` and returns
        an :class:`EvalComparison`. Trumps the canned attributes when set.
    calls:
        Read-only list of every :class:`EvalRequest` ``evaluate()`` saw.
    """

    candidate_scores: ScoreSet | None = None
    baseline_scores: ScoreSet | None = None
    n_examples: int = 120
    eval_callable: Callable[[EvalRequest], EvalComparison] | None = None
    calls: list[EvalRequest] = field(default_factory=list)

    def evaluate(self, request: EvalRequest) -> EvalComparison:
        self.calls.append(request)
        if self.eval_callable is not None:
            return self.eval_callable(request)
        candidate = self.candidate_scores or _default_candidate_scores()
        candidate = apply_scorer_weights(candidate, request.scorer_weights)
        # If the test set baseline_scores to None explicitly via an
        # attribute write *after* construction, treat it as cold-start.
        # Default at construction time produces a baseline so the default
        # comparison includes deltas.
        baseline: ScoreSet | None = (
            self.baseline_scores if self.baseline_scores else _default_baseline_scores()
        )
        if baseline is not None:
            baseline = apply_scorer_weights(baseline, request.scorer_weights)
        if request.baseline_content is None:
            # Cold start: no baseline to compare against, regardless of attr.
            baseline = None
        return _build_comparison(
            candidate=candidate,
            baseline=baseline,
            eval_dataset_id=request.eval_dataset_id,
            n_examples=self.n_examples,
        )


def _default_candidate_scores() -> ScoreSet:
    return ScoreSet(
        overall=0.94,
        dimensions={"factual": 0.97, "tool_use": 0.93, "tone": 0.89, "safety": 0.99},
    )


def _default_baseline_scores() -> ScoreSet:
    return ScoreSet(
        overall=0.88,
        dimensions={"factual": 0.88, "tool_use": 0.84, "tone": 0.89, "safety": 0.99},
    )


def _build_comparison(
    *,
    candidate: ScoreSet,
    baseline: ScoreSet | None,
    eval_dataset_id: str,
    n_examples: int,
) -> EvalComparison:
    deltas: dict[str, float] = {}
    if baseline is not None:
        deltas["overall"] = round(candidate.overall - baseline.overall, 4)
        for dim, score in candidate.dimensions.items():
            baseline_dim = baseline.dimensions.get(dim)
            if baseline_dim is not None:
                deltas[dim] = round(score - baseline_dim, 4)
    return EvalComparison(
        candidate=candidate,
        baseline=baseline,
        deltas=deltas,
        eval_dataset_id=eval_dataset_id,
        n_examples=n_examples,
    )
