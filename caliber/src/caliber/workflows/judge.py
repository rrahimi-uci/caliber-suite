"""LLM-judge run scorer for workflow refinement eval (golden-path roadmap, Wave 5.2).

The structural :func:`caliber.workflows.refinement.default_run_scorer` derives
``quality`` purely from run status + guardrail pass flags — it never reads the
agent's actual output. :class:`LLMJudgeScorer` replaces the ``quality`` dimension
with a real LLM judgment over the run output (helpfulness / coherence / safety),
while keeping ``completion_rate`` / ``tool_adherence`` structural.

Everything is guarded and config-gated:

* Off by default (``workflow_llm_judge_enabled=False``) → callers use the
  structural scorer, so behavior is unchanged until an operator opts in.
* The judge runs only for completed runs with non-empty output; failed/empty
  runs keep the structural score.
* Any judge error (LLM/SDK/parse failure) falls back to the structural quality —
  a flaky judge can never crash refinement eval.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from caliber.llm.models import (
    DEFAULT_OPENAI_REASONING_EFFORT,
    is_reasoning_model,
    reasoning_effort_for_model,
)
from caliber.secrets import resolve_secret
from caliber.workflows.refinement import RunScorer, default_run_scorer
from caliber.workflows.runtime import WorkflowRunResult

if TYPE_CHECKING:
    from caliber.config import CaliberConfig

logger = logging.getLogger(__name__)

# output text -> quality score in [0, 1]
# (input_text, output_text) -> quality score in [0, 1]
JudgeFn = Callable[[str, str], float]

_JUDGE_SYSTEM = (
    "You are a strict evaluator. Given a user QUESTION and an AI agent's RESPONSE, "
    "rate how well the RESPONSE answers the QUESTION on a 0.0 to 1.0 scale, "
    "considering: on-topic relevance, correctness/factual plausibility, "
    "helpfulness, and safety. A fluent but off-topic or incorrect response scores "
    "low. Respond with ONLY a single number between 0 and 1."
)
_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", re.IGNORECASE)
# Tolerance for clamping float rounding noise (e.g. 1.0000001) into [0, 1];
# values further out than this are treated as a misparse and rejected.
_SCORE_TOLERANCE = 1e-6


def _parse_score(text: str) -> float:
    """Parse a strict 0..1 score, or raise.

    Raises (→ structural fallback) on no number, an ambiguous multi-number reply
    (e.g. ``"8/10"``, ``"version 3.5 quality 0.9"``), or an out-of-range value
    (e.g. ``"95%"``) — so a misparse never silently clamps to a perfect score and
    inflates the promotion gate.
    """
    matches = _NUMBER_RE.findall((text or "").strip())
    if not matches:
        raise ValueError(f"judge returned no number: {text!r}")
    if len(matches) > 1:
        raise ValueError(f"judge returned an ambiguous multi-number reply: {text!r}")
    value = float(matches[0])
    if -_SCORE_TOLERANCE <= value <= 1.0 + _SCORE_TOLERANCE:
        return max(0.0, min(1.0, value))  # clamp only float rounding noise
    raise ValueError(f"judge score out of [0,1]: {value!r}")


class LLMJudgeScorer:
    """A :data:`RunScorer` that LLM-judges ``quality`` over the run output.

    Receives the run input via the optional ``input_text`` kwarg (threaded by the
    refinement replay) so the judge can assess on-topic correctness, not just
    intrinsic fluency.
    """

    def __init__(self, judge_fn: JudgeFn) -> None:
        self._judge_fn = judge_fn

    def __call__(self, result: WorkflowRunResult, *, input_text: str = "") -> dict[str, float]:
        structural = default_run_scorer(result)
        if result.status != "completed":
            return structural
        output = (result.output or "").strip()
        if not output:
            return structural
        try:
            quality = float(self._judge_fn(input_text, output))
        except Exception:
            logger.debug("LLM judge failed; falling back to structural quality", exc_info=True)
            return structural
        quality = max(0.0, min(1.0, quality))
        return {**structural, "quality": quality}


def _judge_user_message(input_text: str, output: str) -> str:
    question = (input_text or "").strip() or "(no question provided)"
    return f"QUESTION:\n{question}\n\nRESPONSE:\n{output}"


def _openai_judge_fn(
    api_key: str,
    model: str,
    reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT,
) -> JudgeFn:
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(api_key=api_key)

    def _judge(input_text: str, output: str) -> float:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": _judge_user_message(input_text, output)},
            ],
        }
        # Reasoning models (o-series, gpt-5*) reject a custom temperature (400) and
        # need room to reason, so omit both — mirroring the workflow executor. Plain
        # chat models keep the deterministic temp + a tight cap on the numeric reply.
        if not is_reasoning_model(model):
            kwargs["temperature"] = 0.0
            kwargs["max_tokens"] = 8
        elif effort := reasoning_effort_for_model(model, reasoning_effort):
            kwargs["reasoning_effort"] = effort
        response = client.chat.completions.create(**kwargs)
        return _parse_score(response.choices[0].message.content or "")

    return _judge


def _anthropic_judge_fn(api_key: str, model: str) -> JudgeFn:
    from anthropic import Anthropic  # type: ignore[import-not-found]  # noqa: PLC0415

    client = Anthropic(api_key=api_key)

    def _judge(input_text: str, output: str) -> float:
        response = client.messages.create(
            model=model,
            max_tokens=8,
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": _judge_user_message(input_text, output)}],
        )
        blocks = getattr(response, "content", None) or []
        text = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")
        return _parse_score(text)

    return _judge


def _build_judge_fn(provider: str, config: Any) -> JudgeFn | None:
    api_key = resolve_secret(config.llm_api_key_env)
    if not api_key:
        logger.warning(
            "workflow LLM judge enabled but no API key at %r; using structural scorer",
            config.llm_api_key_env,
        )
        return None
    model = config.llm_diagnosis_model
    if provider == "openai":
        return _openai_judge_fn(
            api_key,
            model,
            getattr(config, "llm_reasoning_effort", DEFAULT_OPENAI_REASONING_EFFORT),
        )
    if provider == "anthropic":
        return _anthropic_judge_fn(api_key, model)
    return None


def llm_judge_status(config: CaliberConfig | Any | None) -> dict[str, Any]:
    """Describe whether workflow LLM-judge scoring is currently available."""
    if config is None:
        return {
            "available": False,
            "provider": None,
            "model": None,
            "reason": "Workflow judge configuration is unavailable on this server.",
        }
    enabled = bool(getattr(config, "workflow_llm_judge_enabled", False))
    provider = (getattr(config, "llm_provider", "fake") or "fake").strip().lower()
    model = getattr(config, "llm_diagnosis_model", None)
    if not enabled:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "reason": "Enable workflow_llm_judge_enabled to use LLM judge scoring.",
        }
    if provider in ("", "fake", "deterministic"):
        return {
            "available": False,
            "provider": provider or "fake",
            "model": model,
            "reason": "Configure a real LLM provider to enable workflow judge scoring.",
        }
    if provider not in {"openai", "anthropic"}:
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "reason": f"Provider {provider!r} is not supported for workflow judge scoring.",
        }
    key_env = getattr(config, "llm_api_key_env", "")
    if not resolve_secret(key_env):
        return {
            "available": False,
            "provider": provider,
            "model": model,
            "reason": f"Set {key_env} to enable workflow judge scoring.",
        }
    return {
        "available": True,
        "provider": provider,
        "model": model,
        "reason": None,
    }


def build_llm_judge_scorer(config: CaliberConfig | None) -> RunScorer | None:
    """Build an LLM-judge scorer from config, or ``None`` to use the structural one.

    Returns ``None`` (→ caller falls back to ``default_run_scorer``) unless
    ``workflow_llm_judge_enabled`` is set AND a real provider with a key is
    configured.
    """
    status = llm_judge_status(config)
    if not status["available"]:
        return None
    provider = str(status["provider"] or "")
    judge_fn = _build_judge_fn(provider, config)
    if judge_fn is None:
        return None
    return LLMJudgeScorer(judge_fn)
