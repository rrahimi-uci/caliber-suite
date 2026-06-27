"""Preview-run tool sandboxing (plan §26.3, §19.7).

Preview runs must never cause real side effects. This module decides, per tool,
whether the real callable may run or whether a schema-valid mock stands in:

* ``write`` / ``external_action`` tools are **always** mocked — no opt-in.
* ``read`` tools are mocked unless the tool opts in with ``allow_in_preview``.

It also enforces the preview cost guardrail (a per-run token budget) and exposes
a timeout value the runtime applies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from caliber.workflows.ir import IRToolBinding

DEFAULT_PREVIEW_TIMEOUT_S = 30.0
DEFAULT_PREVIEW_TOKEN_BUDGET = 10_000


class PreviewBudgetExceededError(Exception):
    """Raised when a preview run exceeds its token budget (plan §26.3)."""


def should_mock_in_preview(binding: IRToolBinding) -> bool:
    """Return whether a tool must be mocked during preview."""
    if binding.side_effect_level in ("write", "external_action"):
        return True
    # read tools: mocked unless explicitly allowed.
    return not binding.allow_in_preview


def mock_response_for(binding: IRToolBinding) -> dict[str, Any]:
    """Build a schema-valid placeholder response for a mocked tool.

    Uses the tool's ``output_schema`` when present to produce a plausible,
    type-correct stub; otherwise returns a generic marker. Never executes the
    real callable.
    """
    schema = getattr(binding, "output_schema", None)
    if isinstance(schema, dict):
        sample = _sample_from_schema(schema)
        if isinstance(sample, dict):
            sample.setdefault("_preview_mock", True)
            return sample
        return {"value": sample, "_preview_mock": True}
    return {"_preview_mock": True, "tool": binding.registry_ref}


def make_preview_callable(
    binding: IRToolBinding,
    real_callable: Callable[..., Any] | None,
) -> Callable[..., Any]:
    """Wrap a tool callable for preview, mocking when side effects are unsafe."""
    if should_mock_in_preview(binding) or real_callable is None:
        response = mock_response_for(binding)

        def _mock(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return dict(response)

        return _mock
    return real_callable


def _sample_from_schema(schema: dict[str, Any]) -> Any:
    """Produce a minimal value satisfying a small JSON-Schema subset."""
    schema_type = schema.get("type")
    if schema_type == "object":
        props = schema.get("properties", {})
        return {key: _sample_from_schema(sub) for key, sub in props.items()}
    if schema_type == "array":
        return []
    if schema_type == "string":
        return schema.get("example", "preview")
    if schema_type in ("number", "integer"):
        return 0
    if schema_type == "boolean":
        return False
    return None


class TokenBudget:
    """Simple accumulating token budget for preview cost enforcement."""

    def __init__(self, limit: int = DEFAULT_PREVIEW_TOKEN_BUDGET) -> None:
        self.limit = limit
        self.used = 0

    def charge(self, tokens: int) -> None:
        self.used += max(0, tokens)
        if self.limit and self.used > self.limit:
            raise PreviewBudgetExceededError(
                f"preview token budget exceeded: used {self.used} > limit {self.limit}"
            )


__all__ = [
    "DEFAULT_PREVIEW_TIMEOUT_S",
    "DEFAULT_PREVIEW_TOKEN_BUDGET",
    "PreviewBudgetExceededError",
    "TokenBudget",
    "make_preview_callable",
    "mock_response_for",
    "should_mock_in_preview",
]
