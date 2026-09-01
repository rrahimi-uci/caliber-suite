"""Repository-wide defaults and capability checks for OpenAI models."""

from __future__ import annotations

import re
from typing import Literal, cast

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_OPENAI_REASONING_EFFORT: ReasoningEffort = "high"
OPENAI_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

_REASONING_MODEL_RE = re.compile(r"^o\d", re.IGNORECASE)


def is_reasoning_model(model: str | None) -> bool:
    """Whether ``model`` is an OpenAI reasoning model (o-series or ``gpt-5*``)."""
    lowered = (model or "").lower()
    return bool(_REASONING_MODEL_RE.match(lowered)) or "gpt-5" in lowered


def supports_temperature(model: str | None) -> bool:
    """Whether ``model`` accepts a custom sampling temperature."""
    return not is_reasoning_model(model)


def reasoning_effort_for_model(model: str | None, effort: str | None) -> ReasoningEffort | None:
    """Return a normalized OpenAI reasoning effort when the model accepts it."""
    normalized = (effort or "").strip().lower()
    if is_reasoning_model(model) and normalized in OPENAI_REASONING_EFFORTS:
        return cast(ReasoningEffort, normalized)
    return None
