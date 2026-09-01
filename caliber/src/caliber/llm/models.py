"""Shared model-capability detection — one source of truth.

The "is this an OpenAI reasoning model?" check was copy-pasted (with drift) across
the workflow executor, the assistant engine, the eval completion path, and the
refinement judge. Reasoning models (the o-series and any ``gpt-5`` variant) reject
a custom sampling ``temperature`` (only the default is allowed — sending one is a
400) and instead accept a ``reasoning`` effort parameter. Centralizing the check
keeps those call sites in agreement and prevents the latent "judge 400s on a
reasoning model" class of bug.
"""

from caliber.model_defaults import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    OPENAI_REASONING_EFFORTS,
    ReasoningEffort,
    is_reasoning_model,
    reasoning_effort_for_model,
    supports_temperature,
)

__all__ = [
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_OPENAI_REASONING_EFFORT",
    "OPENAI_REASONING_EFFORTS",
    "ReasoningEffort",
    "is_reasoning_model",
    "reasoning_effort_for_model",
    "supports_temperature",
]
