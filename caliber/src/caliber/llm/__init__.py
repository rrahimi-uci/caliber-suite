"""LLM provider abstraction shared across orchestrator stages.

The orchestrator stages that need LLM work (diagnosis, candidate generation,
triage classifier when it graduates from its deterministic stub) all depend
on the :class:`LLMProvider` Protocol declared here. Two implementations ship:

* :class:`FakeLLMProvider` — deterministic test double used by every unit
  test in the repository.
* :class:`OpenAIAgentsLLMProvider` — production wrapper around the OpenAI
  Agents SDK. Requires the ``[llm]`` install extra.

Selection is config-driven via :class:`caliber.config.CaliberConfig.llm_provider`.
:func:`build_provider` is the one place that does the construction lookup.
"""

from __future__ import annotations

from caliber.llm.circuit_breaker import (
    CircuitBreakerLLMProvider,
    CircuitState,
    LLMCircuitOpenError,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import (
    Diagnosis,
    EvidenceContext,
    LLMProvider,
    LLMProviderError,
    LLMUsage,
    build_provider,
)

__all__ = [
    "CircuitBreakerLLMProvider",
    "CircuitState",
    "Diagnosis",
    "EvidenceContext",
    "FakeLLMProvider",
    "LLMCircuitOpenError",
    "LLMProvider",
    "LLMProviderError",
    "LLMUsage",
    "build_provider",
]
