"""Compatibility checks for the optional OpenAI Agents SDK integration."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any, Final

OPENAI_AGENTS_MIN_VERSION: Final = "0.9.0"
"""The first SDK release with every FunctionTool capability CALIBER uses."""

FUNCTION_TOOL_REQUIRED_PARAMETERS: Final = (
    "needs_approval",
    "timeout_seconds",
    "timeout_behavior",
)
"""Keyword parameters used when CALIBER builds a governed FunctionTool."""

AGENT_REQUIRED_PARAMETERS: Final = ("tool_use_behavior",)
"""Keyword parameters used when CALIBER builds an Agents SDK Agent."""


class OpenAIAgentsSDKCompatibilityError(RuntimeError):
    """The installed SDK cannot preserve the adapter's governed tool contract."""

    missing_capabilities: tuple[str, ...]

    def __init__(self, missing_capabilities: Iterable[str]) -> None:
        self.missing_capabilities = tuple(missing_capabilities)
        details = ", ".join(self.missing_capabilities) or "an unknown capability"
        super().__init__(
            "The installed openai-agents SDK is missing CALIBER-required capabilities: "
            f"{details}. Install openai-agents>={OPENAI_AGENTS_MIN_VERSION}."
        )


def _missing_parameters(target: Any, required: Iterable[str], *, label: str) -> tuple[str, ...]:
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return tuple(f"{label}.{name}" for name in required)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return ()
    return tuple(f"{label}.{name}" for name in required if name not in parameters)


def require_supported_sdk(*, agent_cls: Any, function_tool_cls: Any) -> None:
    """Fail closed unless the SDK exposes every capability the adapter relies on.

    The check is signature-based instead of version-string-based so a vendor or
    local build with the same API remains usable, while an older SDK cannot fail
    later with a raw ``TypeError`` during tool registration.
    """

    missing = (
        *_missing_parameters(
            function_tool_cls,
            FUNCTION_TOOL_REQUIRED_PARAMETERS,
            label="FunctionTool",
        ),
        *_missing_parameters(agent_cls, AGENT_REQUIRED_PARAMETERS, label="Agent"),
    )
    if missing:
        raise OpenAIAgentsSDKCompatibilityError(missing)


__all__ = [
    "AGENT_REQUIRED_PARAMETERS",
    "FUNCTION_TOOL_REQUIRED_PARAMETERS",
    "OPENAI_AGENTS_MIN_VERSION",
    "OpenAIAgentsSDKCompatibilityError",
    "require_supported_sdk",
]
