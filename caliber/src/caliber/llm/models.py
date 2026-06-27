"""Shared model-capability detection — one source of truth.

The "is this an OpenAI reasoning model?" check was copy-pasted (with drift) across
the workflow executor, the assistant engine, the eval completion path, and the
refinement judge. Reasoning models (the o-series and any ``gpt-5`` variant) reject
a custom sampling ``temperature`` (only the default is allowed — sending one is a
400) and instead accept a ``reasoning`` effort parameter. Centralizing the check
keeps those call sites in agreement and prevents the latent "judge 400s on a
reasoning model" class of bug.
"""

from __future__ import annotations

import re

# o-series ids start ``o1``/``o3``/``o4``/… ; gpt-5* are reasoning models too.
_REASONING_MODEL_RE = re.compile(r"^o\d", re.IGNORECASE)


def is_reasoning_model(model: str | None) -> bool:
    """Whether ``model`` is an OpenAI reasoning model (o-series or ``gpt-5*``)."""
    lowered = (model or "").lower()
    return bool(_REASONING_MODEL_RE.match(lowered)) or "gpt-5" in lowered


def supports_temperature(model: str | None) -> bool:
    """Whether ``model`` accepts a custom sampling ``temperature`` (reasoning models don't)."""
    return not is_reasoning_model(model)
