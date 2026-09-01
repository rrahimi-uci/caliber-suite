"""Retrieval-quality scoring for knowledge-base calibration (Phase K1).

A *calibration run* scores one knowledge-base version against a test-question
set on four metrics:

* **Recall@k** (deterministic) — of the gold sources for a question, the
  fraction whose source appears among the top-k retrieved chunks' sources.
* **nDCG@k** (deterministic) — binary relevance (a retrieved chunk is relevant
  iff its source is a gold source), standard DCG/IDCG over the top-k ranking.
  Rewards putting relevant chunks *earlier*, so it differs from precision.
* **Faithfulness** (LLM-judged, 0..1) — is the generated answer supported by the
  retrieved chunk texts? No gold needed.
* **Answer-correctness** (LLM-judged, 0..1) — generated answer vs the gold
  answer. Skipped when the example carries no gold answer.

The deterministic retrieval math (:func:`recall_at_k`, :func:`ndcg_at_k`) is
**pure** — it takes plain lists of normalized source strings and is unit-testable
without any LLM, database, or HTTP. The LLM-judged metrics go through an
*injectable* judge (``CompletionFn``) so tests pass a deterministic fake.

The whole per-question step — retrieve, generate an answer, judge it — is funneled
through a single injectable callable (:data:`QuestionRunner`) so the route can
stub it in tests while the metric aggregation and the deterministic math stay
exercised by real code.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from caliber.eval.judge_scorer import JudgeError, build_judge, score_with_judge

# (system, user) -> completion text. Mirrors ``caliber.eval.predict.CompletionFn``.
# Retained for back-compat; KB judging now goes through :class:`KbJudge`.
CompletionFn = Callable[[str, str], str]

# Verdict thresholds. A question's verdict is derived from the mean of its
# *defined* metrics (those that aren't None): >= PASS is a pass, >= PARTIAL is a
# partial, below that is a fail. A question with no defined metric at all is a
# "fail" — there was nothing to score.
DEFAULT_PASS_THRESHOLD = 0.7
DEFAULT_PARTIAL_THRESHOLD = 0.4

# A judge reply above 1 but no greater than this is treated as a percentage
# ("85" → 0.85) rather than clamped to 1.0.
_PERCENT_UPPER_BOUND = 100.0

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Source normalization + matching
# ---------------------------------------------------------------------------


def normalize_source(value: str | None) -> str:
    """Normalize a source identifier for tolerant matching.

    Lower-cases, strips surrounding whitespace, drops a leading ``./``, and
    collapses internal whitespace. Returns ``""`` for falsy input. Applied to
    both the gold sources and every candidate identifier on a retrieved chunk so
    a gold ``"FAQ.txt"`` matches a chunk whose ``source_key`` is ``"faq.txt"``.
    """
    if not value:
        return ""
    text = str(value).strip().lower()
    if text.startswith("./"):
        text = text[2:]
    return re.sub(r"\s+", " ", text)


def chunk_source_identifiers(chunk: object) -> set[str]:
    """Collect the normalized identifiers a retrieved chunk can be matched by.

    A chunk's source can be cited in a gold set three ways — by ``source_key``
    (e.g. ``product/guide.md``), by ``source_name`` (the display name), or by the
    ``object_store_path`` URL. We accept any of them, normalized. ``chunk`` may be
    a Pydantic model or a plain mapping/dict-like.
    """
    identifiers: set[str] = set()
    for attr in ("source_key", "source_name", "object_store_path"):
        value = _read_field(chunk, attr)
        normalized = normalize_source(value if isinstance(value, str) else None)
        if normalized:
            identifiers.add(normalized)
    return identifiers


def _read_field(obj: object, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def gold_sources(expected: dict[str, Any] | None) -> list[str]:
    """Extract the normalized gold-source list from an example's ``expected``.

    Convention: ``expected = {"sources": ["<source_key or object_store_path>",
    ...]}``. Returns an empty list when the key is absent or empty — the caller
    treats that as "no gold sources" and skips Recall@k / nDCG@k for the
    question.
    """
    if not expected:
        return []
    raw = expected.get("sources")
    if not isinstance(raw, (list, tuple)):
        return []
    normalized = [normalize_source(item) for item in raw if isinstance(item, str)]
    return [item for item in normalized if item]


def gold_answer(expected: dict[str, Any] | None) -> str | None:
    """Extract the gold answer string from an example's ``expected``.

    Convention: ``expected = {"answer": "..."}``. Returns ``None`` when absent or
    blank — the caller then skips answer-correctness for the question.
    """
    if not expected:
        return None
    raw = expected.get("answer")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


# ---------------------------------------------------------------------------
# Deterministic retrieval metrics — pure functions, no LLM / DB / HTTP.
# ---------------------------------------------------------------------------


def _relevance_flags(retrieved_sources: Sequence[set[str]], gold: Iterable[str]) -> list[int]:
    """Per-rank binary relevance: 1 if a retrieved chunk's identifiers hit gold.

    ``retrieved_sources`` is the top-k ranking, each entry the set of normalized
    identifiers for that rank's chunk. A rank is relevant iff its identifier set
    intersects the gold set.
    """
    gold_set = {item for item in gold if item}
    return [1 if (identifiers & gold_set) else 0 for identifiers in retrieved_sources]


def recall_at_k(retrieved_sources: Sequence[set[str]], gold: Sequence[str], k: int) -> float:
    """Recall@k: fraction of *gold sources* covered by the top-k retrieved chunks.

    Denominator is the number of distinct gold sources; numerator is how many of
    them appear among any of the top-k chunks' identifier sets. Returns ``0.0``
    when there are gold sources but none are retrieved. Raises ``ValueError`` for
    an empty gold set — callers must skip (None) those questions, not score them.
    """
    gold_set = {item for item in gold if item}
    if not gold_set:
        raise ValueError("recall_at_k requires a non-empty gold set")
    top = list(retrieved_sources[: max(k, 0)])
    covered = {g for g in gold_set if any(g in identifiers for identifiers in top)}
    return len(covered) / len(gold_set)


def ndcg_at_k(retrieved_sources: Sequence[set[str]], gold: Sequence[str], k: int) -> float:
    """nDCG@k with binary relevance over the top-k ranking.

    ``DCG = sum_i rel_i / log2(i + 2)`` for i in 0..k-1 (rank 1 → divisor
    ``log2(2)=1``). The ideal DCG places the relevant chunks first; the ideal
    count is the number of relevant chunks actually retrieved in the top-k
    (multiple chunks may match the same gold source, since documents are
    chunked). ``nDCG = DCG / IDCG`` (``0.0`` when IDCG is 0, i.e. nothing
    relevant retrieved) and is bounded to ``[0, 1]``. Raises ``ValueError`` for
    an empty gold set.

    Ranking-sensitive: the same relevant chunks score higher when retrieved
    earlier, so nDCG@k differs from precision@k.
    """
    gold_set = {item for item in gold if item}
    if not gold_set:
        raise ValueError("ndcg_at_k requires a non-empty gold set")
    top = list(retrieved_sources[: max(k, 0)])
    flags = _relevance_flags(top, gold_set)
    dcg = sum(rel / math.log2(rank + 2) for rank, rel in enumerate(flags))
    # IDCG places the relevant items first. The ideal count is the number of
    # relevant items actually retrieved in the top-k (``sum(flags)``), NOT the
    # gold-set size: in RAG one gold document is split into many chunks, so
    # several top-k chunks can match the SAME gold source. Capping IDCG at the
    # gold-set size made DCG exceed IDCG and pushed nDCG above 1.0.
    ideal_hits = round(sum(flags))
    idcg = sum(1.0 / math.log2(rank + 2) for rank in range(ideal_hits))
    if idcg == 0:
        return 0.0
    return dcg / idcg


# ---------------------------------------------------------------------------
# LLM-judged metrics — go through the unified judge path (mlflow.genai.make_judge).
# ---------------------------------------------------------------------------

# make_judge instructions (must reference the eval template vars). The answer is
# ``{{ outputs }}``; faithfulness reads the retrieved context from ``{{ inputs }}``;
# correctness reads the gold answer from ``{{ expectations }}``.
_FAITHFULNESS_INSTRUCTIONS = (
    "You are a strict RAG faithfulness judge. The generated answer is {{ outputs }} "
    "and the retrieved context it was supposed to rely on is {{ inputs }}. Rate how "
    "well the answer is supported by — and only by — that context, as a number "
    "between 0 and 1: 1 means every claim is grounded in the context, 0 means the "
    "answer is unsupported or contradicts the context."
)

_CORRECTNESS_INSTRUCTIONS = (
    "You are a strict answer-correctness judge. The generated answer is {{ outputs }} "
    "and the reference (gold) answer is {{ expectations }}. Rate how correct and "
    "complete the generated answer is relative to the reference, as a number between "
    "0 and 1: 1 means fully correct, 0 means wrong."
)


def parse_score(text: str | None) -> float | None:
    """Parse a 0..1 score from a judge reply, or ``None`` if unparseable.

    Pulls the first numeric token from the reply and clamps it to ``[0, 1]`` (a
    judge that answers ``"score: 0.8/1"`` still resolves to ``0.8``). A bare
    whole number above 1 and no greater than 100 (no decimal point) is read as a
    percentage — ``"85"`` → ``0.85`` — while a fractional overflow like ``"3.0"``
    is simply clamped to ``1.0``, so the percentage path stays unambiguous.
    Returns ``None`` when the reply contains no number at all, so an
    uncooperative judge yields an undefined metric rather than a fabricated 0.
    """
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    token = match.group()
    value = float(token)
    if "." not in token and 1.0 < value <= _PERCENT_UPPER_BOUND:
        # Tolerate a whole-number percentage reply ("85" meaning 0.85).
        value = value / 100.0
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class KbJudge:
    """Scores KB calibration's two judged metrics through the unified judge path.

    Wraps two ``mlflow.genai.make_judge`` judges — built via
    :func:`caliber.eval.judge_scorer.build_judge` and run via
    :func:`score_with_judge` — so faithfulness/answer-correctness use the exact
    same judge machinery as the Evaluations scorecard and the optimization gate
    (the bespoke completion + regex judge is gone). The metric guards (no answer
    / no context / no gold → ``None``) live here; a judge that errors degrades to
    ``None`` so a calibration run still yields its deterministic retrieval scores.
    """

    faithfulness_judge: Any
    correctness_judge: Any

    def faithfulness(self, answer: str | None, contexts: Sequence[str]) -> float | None:
        clean_contexts = [c for c in contexts if c and c.strip()]
        if not answer or not answer.strip() or not clean_contexts:
            return None
        joined = "\n\n".join(f"[{i + 1}] {c.strip()}" for i, c in enumerate(clean_contexts))
        try:
            return score_with_judge(
                self.faithfulness_judge,
                inputs={"retrieved_context": joined},
                outputs=answer.strip(),
            ).score
        except JudgeError:
            return None

    def correctness(self, answer: str | None, reference: str | None) -> float | None:
        if reference is None or not reference.strip():
            return None
        if not answer or not answer.strip():
            return 0.0
        try:
            return score_with_judge(
                self.correctness_judge,
                inputs={},
                outputs=answer.strip(),
                expectations={"reference": reference.strip()},
            ).score
        except JudgeError:
            return None


def build_kb_judge(model: str | None = None) -> KbJudge | None:
    """Build the faithfulness + correctness judges via the unified judge path.

    Returns ``None`` when the judges can't be built (``mlflow.genai`` unavailable),
    so a calibration run degrades to its deterministic retrieval metrics rather
    than failing. ``model`` is an optional MLflow model id; ``None`` inherits
    CALIBER's ``gpt-5.6-luna`` default with high reasoning.
    """
    try:
        faithfulness = build_judge(
            "kb-faithfulness",
            _FAITHFULNESS_INSTRUCTIONS,
            model=model,
            feedback_value_type="float",
        )
        correctness = build_judge(
            "kb-answer-correctness",
            _CORRECTNESS_INSTRUCTIONS,
            model=model,
            feedback_value_type="float",
        )
    except JudgeError:
        return None
    return KbJudge(faithfulness_judge=faithfulness, correctness_judge=correctness)


# ---------------------------------------------------------------------------
# Per-question result + verdict + aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalOutcome:
    """The retrieval + answer for one question — the output of a ``QuestionRunner``.

    ``retrieved_sources`` is the ranked list (top-k order) of normalized
    identifier sets per chunk; ``retrieved_chunk_texts`` is the parallel list of
    chunk contents (for the faithfulness judge); ``retrieved_source_keys`` is a
    flat, display-friendly list of the primary identifiers surfaced (for the
    persisted per-question result).
    """

    answer: str | None
    retrieved_sources: list[set[str]]
    retrieved_chunk_texts: list[str]
    retrieved_source_keys: list[str]
    answer_error: str | None = None


# A QuestionRunner turns (question, top_k, retrieval_mode) into a RetrievalOutcome.
# The route's default implementation calls KnowledgeBaseService.query; tests pass
# a deterministic stub so the metric math + aggregation run against fixed inputs.
QuestionRunner = Callable[[str, int, str], RetrievalOutcome]


@dataclass
class QuestionScore:
    """Scored result for one calibration question."""

    question: str
    recall_at_k: float | None
    ndcg_at_k: float | None
    faithfulness: float | None
    answer_correctness: float | None
    verdict: str
    score: float | None
    answer: str | None
    answer_error: str | None
    gold_sources: list[str] = field(default_factory=list)
    retrieved_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "recall_at_k": self.recall_at_k,
            "ndcg_at_k": self.ndcg_at_k,
            "faithfulness": self.faithfulness,
            "answer_correctness": self.answer_correctness,
            "verdict": self.verdict,
            "score": self.score,
            "answer": self.answer,
            "answer_error": self.answer_error,
            "gold_sources": list(self.gold_sources),
            "retrieved_sources": list(self.retrieved_sources),
        }


def _defined(values: Iterable[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def derive_verdict(
    metrics: Sequence[float | None],
    *,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    partial_threshold: float = DEFAULT_PARTIAL_THRESHOLD,
) -> tuple[str, float | None]:
    """Verdict + composite score from a question's available metrics.

    The composite is the mean of the *defined* metrics (None entries ignored). A
    question with no defined metric scores ``None`` and a ``"fail"`` verdict
    (nothing to score). Otherwise: ``>= pass_threshold`` → ``"pass"``, ``>=
    partial_threshold`` → ``"partial"``, else ``"fail"``.
    """
    defined = _defined(metrics)
    if not defined:
        return "fail", None
    composite = sum(defined) / len(defined)
    if composite >= pass_threshold:
        verdict = "pass"
    elif composite >= partial_threshold:
        verdict = "partial"
    else:
        verdict = "fail"
    return verdict, composite


def score_question(
    *,
    question: str,
    expected: dict[str, Any] | None,
    outcome: RetrievalOutcome,
    top_k: int,
    judge: KbJudge | None,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    partial_threshold: float = DEFAULT_PARTIAL_THRESHOLD,
) -> QuestionScore:
    """Score one question across all four metrics and derive its verdict.

    Deterministic metrics (Recall@k, nDCG@k) are computed from ``outcome`` and
    the gold sources; they are ``None`` when the example has no gold sources. The
    LLM-judged metrics are ``None`` when no judge is supplied (so a run without a
    configured judge still yields well-formed retrieval scores).
    """
    gold = gold_sources(expected)
    reference = gold_answer(expected)

    recall: float | None = None
    ndcg: float | None = None
    if gold:
        recall = recall_at_k(outcome.retrieved_sources, gold, top_k)
        ndcg = ndcg_at_k(outcome.retrieved_sources, gold, top_k)

    faithfulness: float | None = None
    correctness: float | None = None
    if judge is not None:
        faithfulness = judge.faithfulness(outcome.answer, outcome.retrieved_chunk_texts)
        correctness = judge.correctness(outcome.answer, reference)

    verdict, composite = derive_verdict(
        [recall, ndcg, faithfulness, correctness],
        pass_threshold=pass_threshold,
        partial_threshold=partial_threshold,
    )
    return QuestionScore(
        question=question,
        recall_at_k=recall,
        ndcg_at_k=ndcg,
        faithfulness=faithfulness,
        answer_correctness=correctness,
        verdict=verdict,
        score=composite,
        answer=outcome.answer,
        answer_error=outcome.answer_error,
        gold_sources=gold,
        retrieved_sources=list(outcome.retrieved_source_keys),
    )


def _mean_or_none(values: Iterable[float | None]) -> float | None:
    defined = _defined(values)
    if not defined:
        return None
    return sum(defined) / len(defined)


def aggregate_metrics(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """Aggregate per-question scores into the durable ``metrics`` JSON bag.

    Each metric is averaged over the questions where it is *defined* (None
    entries excluded), so a metric stays meaningful even when some questions
    couldn't be scored on it. Also carries the pass/partial/fail counts and the
    mean composite score.
    """
    passed = sum(1 for s in scores if s.verdict == "pass")
    partial = sum(1 for s in scores if s.verdict == "partial")
    failed = sum(1 for s in scores if s.verdict == "fail")
    return {
        "recall_at_k": _mean_or_none(s.recall_at_k for s in scores),
        "ndcg_at_k": _mean_or_none(s.ndcg_at_k for s in scores),
        "faithfulness": _mean_or_none(s.faithfulness for s in scores),
        "answer_correctness": _mean_or_none(s.answer_correctness for s in scores),
        "overall_score": _mean_or_none(s.score for s in scores),
        "passed_count": passed,
        "partial_count": partial,
        "failed_count": failed,
        "question_count": len(scores),
    }


@dataclass(frozen=True)
class CalibrationOutcome:
    """The full result of scoring a question set — aggregate + per-question."""

    metrics: dict[str, Any]
    results: list[dict[str, Any]]


def run_calibration(
    *,
    questions: Sequence[tuple[str, dict[str, Any] | None]],
    runner: QuestionRunner,
    top_k: int,
    retrieval_mode: str,
    judge: KbJudge | None,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    partial_threshold: float = DEFAULT_PARTIAL_THRESHOLD,
) -> CalibrationOutcome:
    """Score every question and aggregate — the calibration entry point.

    ``questions`` is a list of ``(question_text, expected_dict_or_None)``. For
    each, the injectable ``runner`` performs retrieval + answer generation
    (``RetrievalOutcome``); :func:`score_question` then scores it. The route
    supplies a ``runner`` backed by ``KnowledgeBaseService.query``; tests supply
    a deterministic stub.
    """
    scores: list[QuestionScore] = []
    for question, expected in questions:
        outcome = runner(question, top_k, retrieval_mode)
        scores.append(
            score_question(
                question=question,
                expected=expected,
                outcome=outcome,
                top_k=top_k,
                judge=judge,
                pass_threshold=pass_threshold,
                partial_threshold=partial_threshold,
            )
        )
    return CalibrationOutcome(
        metrics=aggregate_metrics(scores),
        results=[score.to_dict() for score in scores],
    )
