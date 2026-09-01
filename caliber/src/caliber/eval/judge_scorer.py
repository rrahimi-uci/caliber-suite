"""The one judge path: build + run an LLM judge via ``mlflow.genai.make_judge``.

CALIBER had three disjoint scoring engines — the deterministic Evaluations
scorecard, the optimization/refinement gate (which alone used ``make_judge``),
and hand-rolled per-feature judges (KB faithfulness/answer-correctness). This
module is the single seam every judge-backed scorer now goes through:

* :func:`build_judge` wraps ``mlflow.genai.make_judge`` (one import, one set of
  failure semantics) and returns the judge object — which is both an
  ``mlflow.genai`` ``Scorer`` (usable in ``mlflow.genai.evaluate``) and a
  callable for direct per-example scoring.
* :func:`score_with_judge` invokes a built judge on literal
  ``inputs``/``outputs``/``expectations`` fields and coerces the returned
  ``Feedback`` into a ``[0, 1]`` float plus its raw value + rationale — the shape
  the scorecard and the calibration loops need.

The mlflow import stays lazy so the unit-test suite need not pull in
``mlflow.genai`` to import callers, and a deterministic fake judge (whose
``__call__`` returns a stub ``Feedback``) drops straight in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from caliber.llm.models import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    reasoning_effort_for_model,
)

# ``feedback_value_type`` string -> the Python type ``make_judge`` enforces for
# structured output. Mirrors ``eval.mlflow_runner._JUDGE_VALUE_TYPE_MAP`` (kept
# in sync; that module now imports this one).
JUDGE_VALUE_TYPE_MAP: dict[Any, type] = {
    "bool": bool,
    "int": int,
    "float": float,
    "str": str,
}

# String verdicts a judge might return in lieu of a number, mapped to a unit
# score. Lower-cased before lookup.
_AFFIRMATIVE = frozenset({"pass", "passed", "yes", "true", "correct", "faithful", "good"})
_NEGATIVE = frozenset({"fail", "failed", "no", "false", "incorrect", "unfaithful", "bad"})


class JudgeError(Exception):
    """Raised when a judge cannot be built or its verdict cannot be scored."""


@dataclass(frozen=True)
class JudgeOutcome:
    """The result of running a judge on one example.

    ``score`` is always a ``[0, 1]`` float (the scorecard / calibration contract);
    ``value`` is the judge's raw feedback value (bool/number/str); ``rationale``
    is the judge's natural-language justification when it provides one.
    """

    score: float
    value: Any
    rationale: str | None


def build_judge(
    name: str,
    instructions: str,
    *,
    model: str | None = None,
    feedback_value_type: str | None = None,
) -> Any:
    """Build a judge via ``mlflow.genai.make_judge``.

    ``name`` is the bare judge name (callers that carry a ``Judge.<name>`` token
    should strip the prefix first). Raises :class:`JudgeError` if instructions
    are empty, mlflow is too old, or ``make_judge`` rejects the definition.
    """
    if not isinstance(instructions, str) or not instructions.strip():
        raise JudgeError(f"judge {name!r} requires non-empty instructions")
    try:
        from mlflow.genai import make_judge  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - mlflow always present in prod
        raise JudgeError(
            "mlflow.genai.make_judge is unavailable; upgrade mlflow to >=3.14"
        ) from exc

    effective_model = (
        model.strip()
        if isinstance(model, str) and model.strip()
        else f"openai:/{DEFAULT_OPENAI_MODEL}"
    )
    kwargs: dict[str, Any] = {
        "name": name,
        "instructions": instructions,
        "model": effective_model,
    }
    if effort := reasoning_effort_for_model(effective_model, DEFAULT_OPENAI_REASONING_EFFORT):
        kwargs["inference_params"] = {"reasoning_effort": effort}
    value_type = JUDGE_VALUE_TYPE_MAP.get(feedback_value_type)
    if value_type is not None:
        kwargs["feedback_value_type"] = value_type
    try:
        return make_judge(**kwargs)
    except Exception as exc:
        raise JudgeError(f"failed to build judge {name!r}: {exc}") from exc


def coerce_feedback_value(value: Any) -> float:
    """Coerce a judge's raw feedback value into a ``[0, 1]`` float.

    * ``bool`` → 1.0 / 0.0.
    * numbers → clamped to ``[0, 1]`` (a judge authored for the scorecard is
      expected to emit a unit score or a pass/fail bool; out-of-range numbers
      are clamped rather than guessed-at as a Likert scale).
    * strings → a leading float if present, else an affirmative/negative verdict
      word, else a :class:`JudgeError`.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if lowered in _AFFIRMATIVE:
            return 1.0
        if lowered in _NEGATIVE:
            return 0.0
        # A leading number (optionally a percentage), e.g. "0.8", "85%", "4/5".
        token = lowered.rstrip("%")
        try:
            number = float(token)
        except ValueError as exc:
            raise JudgeError(f"cannot score judge verdict {value!r}") from exc
        if lowered.endswith("%"):
            number /= 100.0
        return max(0.0, min(1.0, number))
    raise JudgeError(f"cannot score judge verdict of type {type(value).__name__}")


def score_with_judge(
    judge: Any,
    *,
    inputs: Any,
    outputs: Any,
    expectations: Any | None = None,
) -> JudgeOutcome:
    """Run ``judge`` on one example's fields and return a :class:`JudgeOutcome`.

    Calls ``judge(inputs=…, outputs=…, expectations=…)`` (the ``InstructionsJudge``
    contract) and coerces the returned ``Feedback`` to a unit score. Any failure
    — the judge raising, or an unscoreable verdict — surfaces as
    :class:`JudgeError` so the caller can degrade that single example to an error
    instead of crashing the whole run.
    """
    call_kwargs: dict[str, Any] = {"inputs": inputs, "outputs": outputs}
    if expectations is not None:
        call_kwargs["expectations"] = expectations
    try:
        feedback = judge(**call_kwargs)
    except Exception as exc:
        raise JudgeError(f"judge invocation failed: {exc}") from exc

    value = getattr(feedback, "value", feedback)
    rationale = getattr(feedback, "rationale", None)
    score = coerce_feedback_value(value)
    return JudgeOutcome(
        score=score,
        value=value,
        rationale=rationale if isinstance(rationale, str) else None,
    )
