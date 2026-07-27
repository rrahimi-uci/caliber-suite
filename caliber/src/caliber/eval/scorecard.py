"""Per-example scorecard engine for ad-hoc evaluation runs.

The refinement-pipeline :class:`~caliber.eval.provider.EvalProvider` returns
only an *aggregate* :class:`ScoreSet` (candidate vs. baseline) — enough for the
regression gate, but not enough to render MLflow-style per-row results in the
UI. This module fills that gap: it runs each example of an eval dataset through
a ``predict`` callable and a set of **deterministic** scorers, then folds the
per-row scores into an aggregate.

Determinism is the point. Every scorer here is a pure function of
``(prediction, expected_text)`` so a run is reproducible and unit-testable
without an LLM in the loop — the only non-deterministic part is ``predict``
itself (a real model call), which the route injects. Scores are floats in
``[0, 1]``; a row "passes" when its mean score clears ``pass_threshold``.

This never fabricates a model: the ``llm`` predict target requires a real
provider (see :func:`caliber.eval.predict.build_completion_fn`). The ``reference``
target (predictions == expected) is a labelled diagnostic only.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# A predict callable turns one example's ``input`` dict into a model output
# string. The route builds this from the configured LLM (or a reference echo).
PredictFn = Callable[[Mapping[str, Any]], str]

# A judge runner scores one row given the prediction + the full example context
# (so the judge can reference inputs/expectations), returning a ``[0, 1]`` float.
# The route builds these from ``caliber_judges`` rows via the shared judge path
# (``caliber.eval.judge_scorer``); the scorecard stays decoupled from mlflow and
# unit-testable with a plain callable. Keyed by the ``Judge.<id>`` scorer name.
JudgeRunner = Callable[[str, Mapping[str, Any], Mapping[str, Any]], float]

# Scorer-name prefix marking a custom LLM judge (vs. a deterministic scorer).
JUDGE_SCORER_PREFIX = "Judge."

# Default scoring suite — reference-free scorers that need only the expected
# answer text. ``non_empty`` is available but off by default (too lenient to
# carry a run on its own).
DEFAULT_SCORERS: tuple[str, ...] = ("exact_match", "token_f1", "contains_expected")

# A row passes when its mean score clears this (overridable per run).
DEFAULT_PASS_THRESHOLD = 0.5

# Keys we look at, in order, to pull the "expected answer" string out of an
# example's ``expected`` dict. Mirrors the user-field preference in
# :mod:`caliber.eval.predict` so input/expected extraction stay symmetric.
_EXPECTED_FIELD_PREFERENCE = (
    "expected",
    "answer",
    "output",
    "response",
    "expected_response",
    "expected_output",
    "label",
    "text",
    "value",
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace so trivial formatting doesn't fail a match."""
    return _WS_RE.sub(" ", text.strip().lower())


def expected_text(expected: Mapping[str, Any] | None) -> str:
    """Pull the canonical expected-answer string from an example's ``expected``.

    Prefers a conventional field (``expected``/``answer``/...), then the sole
    string value, then a stable JSON dump so nothing is silently dropped.
    """
    if not expected:
        return ""
    for key in _EXPECTED_FIELD_PREFERENCE:
        value = expected.get(key)
        if isinstance(value, str) and value.strip():
            return value
    string_values = [v for v in expected.values() if isinstance(v, str) and v.strip()]
    if len(string_values) == 1:
        return string_values[0]
    return json.dumps(expected, ensure_ascii=False, sort_keys=True)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# Scorers — each is ``(prediction, expected_text) -> float in [0, 1]``.
# ---------------------------------------------------------------------------


def _score_exact_match(prediction: str, expected: str) -> float:
    return 1.0 if _normalize(prediction) == _normalize(expected) else 0.0


def _score_token_f1(prediction: str, expected: str) -> float:
    """Token-overlap F1 between prediction and expected (SQuAD-style)."""
    pred_tokens = _tokens(prediction)
    exp_tokens = _tokens(expected)
    if not pred_tokens and not exp_tokens:
        return 1.0
    if not pred_tokens or not exp_tokens:
        return 0.0
    # Multiset intersection so repeated words count proportionally.
    common = 0
    remaining = list(exp_tokens)
    for token in pred_tokens:
        if token in remaining:
            remaining.remove(token)
            common += 1
    if common == 0:
        return 0.0
    precision = common / len(pred_tokens)
    recall = common / len(exp_tokens)
    return round(2 * precision * recall / (precision + recall), 4)


def _score_contains_expected(prediction: str, expected: str) -> float:
    norm_expected = _normalize(expected)
    if not norm_expected:
        return 0.0
    return 1.0 if norm_expected in _normalize(prediction) else 0.0


def _score_non_empty(prediction: str, _expected: str) -> float:
    return 1.0 if prediction.strip() else 0.0


_SCORERS: dict[str, Callable[[str, str], float]] = {
    "exact_match": _score_exact_match,
    "token_f1": _score_token_f1,
    "contains_expected": _score_contains_expected,
    "non_empty": _score_non_empty,
}

# Human-facing descriptions for the UI's scorer picker.
AVAILABLE_SCORERS: dict[str, str] = {
    "exact_match": "Prediction equals the expected answer (case/space-insensitive).",
    "token_f1": "Token-overlap F1 between prediction and expected answer.",
    "contains_expected": "Expected answer text appears within the prediction.",
    "non_empty": "Prediction is non-empty.",
}


def resolve_scorers(
    names: Sequence[str] | None,
    *,
    allowed_judges: Sequence[str] = (),
) -> list[str]:
    """Validate + de-duplicate requested scorer names (order-preserving).

    Falls back to :data:`DEFAULT_SCORERS` when ``names`` is empty. A name is
    valid if it's a built-in deterministic scorer **or** a ``Judge.<id>`` token
    the caller has already hydrated (passed in ``allowed_judges``) — the route
    resolves judges against ``caliber_judges`` and passes the accepted tokens
    here so an unknown/archived judge still 400s. Raises :class:`ValueError` on
    any unknown scorer.
    """
    requested = [n for n in (names or []) if isinstance(n, str) and n]
    if not requested:
        return list(DEFAULT_SCORERS)
    allowed_judge_set = set(allowed_judges)
    resolved: list[str] = []
    for name in requested:
        is_judge = name.startswith(JUDGE_SCORER_PREFIX)
        if is_judge and name not in allowed_judge_set:
            raise ValueError(f"unknown or unavailable judge scorer {name!r}")
        if not is_judge and name not in _SCORERS:
            raise ValueError(f"unknown scorer {name!r}; available: {sorted(_SCORERS)}")
        if name not in resolved:
            resolved.append(name)
    return resolved


@dataclass(frozen=True)
class ScorecardRow:
    """One scored example."""

    example_id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    prediction: str
    scores: dict[str, float]
    score: float
    passed: bool
    error: str | None = None
    # Relative importance of this example in the aggregate, carried over from
    # ``CaliberEvalDatasetExample.weight``. 1.0 keeps every legacy call site
    # (and every unweighted dataset) on the plain arithmetic mean.
    weight: float = 1.0
    # Dataset tags for this example, carried through so a scorecard can be
    # sliced by tag downstream instead of losing the label at load time.
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "input": self.input,
            "expected": self.expected,
            "prediction": self.prediction,
            "scores": self.scores,
            "score": self.score,
            "passed": self.passed,
            "error": self.error,
            "weight": self.weight,
            "tags": self.tags,
        }


@dataclass(frozen=True)
class ScorecardResult:
    """Per-row results + folded aggregate for one evaluation pass.

    ``aggregate``, ``overall``, and ``pass_rate`` are **weighted** means over
    each row's example weight, so a curated dataset's weights actually affect
    the verdict. ``passed_count``/``failed_count``/``n_examples`` stay raw row
    counts — they answer "how many examples", not "how much did they matter".
    With every weight at the 1.0 default the two views coincide.
    """

    rows: list[ScorecardRow] = field(default_factory=list)
    scorers: list[str] = field(default_factory=list)
    aggregate: dict[str, float] = field(default_factory=dict)
    overall: float = 0.0
    pass_rate: float = 0.0
    n_examples: int = 0
    passed_count: int = 0
    failed_count: int = 0


def _row_score(scores: Mapping[str, float]) -> float:
    if not scores:
        return 0.0
    return round(sum(scores.values()) / len(scores), 4)


def _example_weight(example: Mapping[str, Any]) -> float:
    """Read an example's aggregate weight, defaulting to 1.0.

    Non-numeric, negative, non-finite, or absent weights fall back to 1.0
    rather than raising: a malformed dataset row should be scored normally, not
    abort the run or poison every aggregate with NaN. A weight of exactly 0 is
    honoured (the row is scored and shown but contributes nothing to the
    aggregate).
    """
    raw = example.get("weight")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 1.0
    value = float(raw)
    if value < 0 or not math.isfinite(value):
        return 1.0
    return value


def run_scorecard(
    examples: Sequence[Mapping[str, Any]],
    predict: PredictFn,
    scorer_names: Sequence[str] | None = None,
    *,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    judge_runners: Mapping[str, JudgeRunner] | None = None,
) -> ScorecardResult:
    """Score every example through ``predict`` + the requested scorers.

    ``examples`` are dicts with ``example_id`` / ``input`` / ``expected`` keys
    (the shape :func:`caliber.routes.evaluations` builds from DB rows). Scorers
    are either built-in deterministic functions of ``(prediction, expected)`` or
    ``Judge.<id>`` LLM judges supplied via ``judge_runners`` (each scores the
    prediction with the full example context). A ``predict`` that raises for one
    example degrades that row to an error without aborting the run; a single
    judge/scorer that raises for one row is recorded on that row's ``error`` and
    omitted from its scores — one bad row (or judge) shouldn't lose the rest.
    A row that recorded an error never counts as ``passed``, though: scoring a
    row on the subset of scorers that happened to survive would let a partially
    evaluated example clear the threshold on incomplete evidence.

    Examples may carry a ``weight`` (default 1.0) mirroring
    ``CaliberEvalDatasetExample.weight``. Aggregates, ``overall``, and
    ``pass_rate`` are weighted means over it, so an unweighted dataset behaves
    exactly as before while a curated one actually honours its weights.
    """
    judge_runners = judge_runners or {}
    scorers = resolve_scorers(scorer_names, allowed_judges=list(judge_runners))
    rows: list[ScorecardRow] = []
    # Per-scorer running totals over rows that produced a value, weighted by
    # each row's example weight.
    scorer_totals: dict[str, float] = dict.fromkeys(scorers, 0.0)
    scorer_weights: dict[str, float] = dict.fromkeys(scorers, 0.0)

    for example in examples:
        example_id = str(example.get("example_id", ""))
        inp = dict(example.get("input") or {})
        expected = dict(example.get("expected") or {})
        gold = expected_text(expected)
        weight = _example_weight(example)
        tags = [str(tag) for tag in (example.get("tags") or [])]

        try:
            prediction = predict(inp)
        except Exception as exc:  # per-row isolation: one bad row shouldn't abort the run
            rows.append(
                ScorecardRow(
                    example_id=example_id,
                    input=inp,
                    expected=expected,
                    prediction="",
                    scores={},
                    score=0.0,
                    passed=False,
                    error=str(exc) or exc.__class__.__name__,
                    weight=weight,
                    tags=tags,
                )
            )
            continue

        prediction = prediction if isinstance(prediction, str) else str(prediction)
        scores: dict[str, float] = {}
        row_error: str | None = None
        for name in scorers:
            try:
                if name in _SCORERS:
                    scores[name] = round(_SCORERS[name](prediction, gold), 4)
                elif name in judge_runners:
                    scores[name] = round(judge_runners[name](prediction, inp, expected), 4)
            except Exception as exc:  # one judge failing shouldn't void the other scores
                row_error = row_error or f"{name}: {str(exc) or exc.__class__.__name__}"
        for name, value in scores.items():
            scorer_totals[name] += value * weight
            scorer_weights[name] += weight
        row_score = _row_score(scores)
        rows.append(
            ScorecardRow(
                example_id=example_id,
                input=inp,
                expected=expected,
                prediction=prediction,
                scores=scores,
                score=row_score,
                # A row whose scorer(s) errored has incomplete evidence: pass it
                # on the surviving subset and a broken judge silently improves
                # the pass rate.
                passed=row_error is None and row_score >= pass_threshold,
                error=row_error,
                weight=weight,
                tags=tags,
            )
        )

    aggregate = {
        name: round(scorer_totals[name] / scorer_weights[name], 4)
        for name in scorers
        if scorer_weights[name] > 0
    }
    passed_count = sum(1 for row in rows if row.passed)
    failed_count = len(rows) - passed_count
    total_weight = sum(row.weight for row in rows)
    if total_weight > 0:
        overall = round(sum(row.score * row.weight for row in rows) / total_weight, 4)
        pass_rate = round(
            sum(row.weight for row in rows if row.passed) / total_weight,
            4,
        )
    else:
        overall = 0.0
        pass_rate = 0.0

    return ScorecardResult(
        rows=rows,
        scorers=scorers,
        aggregate=aggregate,
        overall=overall,
        pass_rate=pass_rate,
        n_examples=len(rows),
        passed_count=passed_count,
        failed_count=failed_count,
    )
