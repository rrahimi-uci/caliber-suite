"""Deterministic eval provider for tests and offline demos.

The fake builds a plausible :class:`EvalComparison` from canned defaults
that tests can override. The default behavior is "candidate slightly
better than baseline on every dimension" — enough to clear a default
regression gate — so a worker end-to-end test passes without bespoke
fixture setup.

**These scores are not measurements.** The numbers are constants, so the fake
is marked simulated (:attr:`FakeEvalProvider.is_simulated`) and every record
that stores its output carries that mark. What it does reflect honestly is the
*shape* of the run: the example count is the real size of the pinned dataset
(when a session factory is wired, as the server does) and the dimensions are the
scorers that were requested, so a three-example dataset is never reported as
"120 examples" and a Correctness run never shows invented "tone" and
"tool_use" scores.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

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

    #: Read by :func:`caliber.eval.provider.describe_provider` so stored results say
    #: what produced them and that the numbers are canned.
    provider_name: ClassVar[str] = "fake"
    is_simulated: ClassVar[bool] = True

    candidate_scores: ScoreSet | None = None
    baseline_scores: ScoreSet | None = None
    #: ``None`` (the default) reports the real size of the pinned dataset when a
    #: session factory is wired, and 120 otherwise (a bare instance in a unit
    #: test). An explicit value always wins.
    n_examples: int | None = None
    eval_callable: Callable[[EvalRequest], EvalComparison] | None = None
    calls: list[EvalRequest] = field(default_factory=list)
    session_factory: Any | None = None
    evidence_rows: tuple[dict[str, Any], ...] | None = None

    def evaluate(self, request: EvalRequest) -> EvalComparison:
        self.calls.append(request)
        if self.eval_callable is not None:
            return self.eval_callable(request)
        candidate = self.candidate_scores or _default_candidate_scores(request.scorer_names)
        candidate = apply_scorer_weights(candidate, request.scorer_weights)
        # If the test set baseline_scores to None explicitly via an
        # attribute write *after* construction, treat it as cold-start.
        # Default at construction time produces a baseline so the default
        # comparison includes deltas.
        baseline: ScoreSet | None = (
            self.baseline_scores
            if self.baseline_scores
            else _default_baseline_scores(request.scorer_names)
        )
        if baseline is not None:
            baseline = apply_scorer_weights(baseline, request.scorer_weights)
        if request.baseline_content is None:
            # Cold start: no baseline to compare against, regardless of attr.
            baseline = None
        example_count = self._example_count(request)
        return _build_comparison(
            candidate=candidate,
            baseline=baseline,
            eval_dataset_id=request.eval_dataset_id,
            n_examples=example_count,
            evidence_rows=self._evidence_rows(candidate, baseline, example_count),
        )

    def _evidence_rows(
        self,
        candidate: ScoreSet,
        baseline: ScoreSet | None,
        example_count: int,
    ) -> tuple[dict[str, Any], ...]:
        """Return deterministic rows for the simulated provider."""
        if self.evidence_rows is not None:
            return tuple(dict(row) for row in self.evidence_rows)
        return tuple(
            {
                "example_id": f"fake-{index}",
                "input": {"example": index},
                "expected": {"example": index},
                "prediction": "simulated",
                "scores": dict(candidate.dimensions),
                "score": candidate.overall,
                "passed": candidate.overall >= _SIMULATED_PASS_THRESHOLD,
                "error": None,
                "weight": 1.0,
                "tags": [],
                "baseline_scores": dict(baseline.dimensions) if baseline else None,
            }
            for index in range(1, example_count + 1)
        )

    def _example_count(self, request: EvalRequest) -> int:
        """The example count to report: explicit, else the real dataset size."""
        if self.n_examples is not None:
            return self.n_examples
        if self.session_factory is None:
            return _LEGACY_DEFAULT_EXAMPLES
        # Imported here: ``caliber.eval.predict`` pulls in the LLM stack, and a bare
        # fake used in a unit test must not need it.
        from caliber.eval.predict import build_db_load_dataset  # noqa: PLC0415

        loader = build_db_load_dataset(self.session_factory)
        try:
            return len(loader(request.eval_dataset_id, request.eval_dataset_version))
        except ValueError:
            # The dataset is missing or empty. A real evaluator refuses to run on
            # that; the simulated one keeps the offline pipeline working but reports
            # the true size (zero) instead of inventing one.
            return 0


_LEGACY_DEFAULT_EXAMPLES = 120
_SIMULATED_PASS_THRESHOLD = 0.5
_CANNED_CANDIDATE_OVERALL = 0.94
_CANNED_BASELINE_OVERALL = 0.88


def _default_candidate_scores(scorer_names: list[str] | None = None) -> ScoreSet:
    if scorer_names:
        return ScoreSet(
            overall=_CANNED_CANDIDATE_OVERALL,
            dimensions=dict.fromkeys(scorer_names, _CANNED_CANDIDATE_OVERALL),
        )
    return ScoreSet(
        overall=_CANNED_CANDIDATE_OVERALL,
        dimensions={"factual": 0.97, "tool_use": 0.93, "tone": 0.89, "safety": 0.99},
    )


def _default_baseline_scores(scorer_names: list[str] | None = None) -> ScoreSet:
    if scorer_names:
        return ScoreSet(
            overall=_CANNED_BASELINE_OVERALL,
            dimensions=dict.fromkeys(scorer_names, _CANNED_BASELINE_OVERALL),
        )
    return ScoreSet(
        overall=_CANNED_BASELINE_OVERALL,
        dimensions={"factual": 0.88, "tool_use": 0.84, "tone": 0.89, "safety": 0.99},
    )


def _build_comparison(
    *,
    candidate: ScoreSet,
    baseline: ScoreSet | None,
    eval_dataset_id: str,
    n_examples: int,
    evidence_rows: tuple[dict[str, Any], ...] = (),
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
        evidence_rows=evidence_rows,
    )
