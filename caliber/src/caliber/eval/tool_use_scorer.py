"""Tool-use scorer — validates that the agent calls expected tools.

Inspired by support-agent eval datasets where each example carries an
``expected_tool`` field.  When the eval dataset includes this field the scorer
checks whether the agent's trace contains a span for the expected tool call.

This is a lightweight, synchronous scorer that operates on the predict-fn
output.  It doesn't inspect real MLflow trace spans (that would require
an async collector); instead it relies on the predict-fn returning a
structured dict that includes a ``tool_calls`` key listing the tools
the agent invoked during that turn.

Scoring rules
~~~~~~~~~~~~~

* If the example has no ``expected_tool`` (or it's ``null``), the example
  scores 1.0 — "no tool expectation, nothing to violate".
* If ``expected_tool`` is set and appears in the response's ``tool_calls``
  list, the example scores 1.0.
* If ``expected_tool`` is set but missing from ``tool_calls``, the example
  scores 0.0.

The overall dimension score is the mean across all examples, giving
operators a single number that answers: "what fraction of expected tool
calls did the agent actually make?"

Integration
~~~~~~~~~~~

Register this scorer alongside the standard CALIBER suite::

    from caliber.eval.tool_use_scorer import ToolUseScorer
    provider = MLflowEvalProvider(scorers=[..., ToolUseScorer()])

Or configure it per-agent via ``optimizer_config.scorers``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolUseResult:
    """Result for a single example."""

    expected: str | None
    actual: list[str]
    score: float
    reason: str


@dataclass
class ToolUseScorerResult:
    """Aggregate result across all examples."""

    overall: float
    total: int
    passed: int
    failed: int
    skipped: int
    details: list[ToolUseResult] = field(default_factory=list)


def score_tool_use(
    examples: list[dict[str, Any]],
    responses: list[dict[str, Any] | str],
) -> ToolUseScorerResult:
    """Score tool-use compliance for a batch of examples.

    Parameters
    ----------
    examples:
        Eval dataset rows.  Each may have an ``expected_tool`` key.
    responses:
        Corresponding agent outputs.  If the predict-fn returns a dict
        with a ``tool_calls`` key (list of tool-name strings), those are
        checked against ``expected_tool``.  Plain-string responses are
        treated as "no tool_calls reported".

    Returns
    -------
    Aggregate + per-example results.
    """
    details: list[ToolUseResult] = []
    passed = 0
    failed = 0
    skipped = 0

    for example, response in zip(examples, responses, strict=False):
        expected_tool = example.get("expected_tool")

        # Extract tool_calls from structured response
        if isinstance(response, dict):
            actual_tools: list[str] = [str(t) for t in response.get("tool_calls", [])]
        else:
            actual_tools = []

        if expected_tool is None or expected_tool == "":
            # No tool expectation — score as pass, mark skipped
            details.append(
                ToolUseResult(
                    expected=None,
                    actual=actual_tools,
                    score=1.0,
                    reason="no tool expectation",
                )
            )
            skipped += 1
        elif expected_tool in actual_tools:
            details.append(
                ToolUseResult(
                    expected=expected_tool,
                    actual=actual_tools,
                    score=1.0,
                    reason="expected tool was called",
                )
            )
            passed += 1
        else:
            details.append(
                ToolUseResult(
                    expected=expected_tool,
                    actual=actual_tools,
                    score=0.0,
                    reason=f"expected {expected_tool!r} but got {actual_tools!r}",
                )
            )
            failed += 1

    total = len(details)
    # Overall: mean of all scores (skipped examples count as 1.0)
    overall = sum(d.score for d in details) / total if total > 0 else 0.0

    return ToolUseScorerResult(
        overall=round(overall, 4),
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        details=details,
    )


class ToolUseScorer:
    """MLflow-compatible scorer that checks tool-use compliance.

    Can be used directly with ``MLflowEvalProvider``'s ``scorers`` list.
    Wraps :func:`score_tool_use` so it can be called per-example by the
    MLflow evaluate harness, or in batch by CALIBER's own eval path.
    """

    name = "tool_use"

    def __call__(
        self,
        *,
        predictions: list[str] | None = None,
        inputs: Any = None,
        outputs: Any = None,
        expectations: Any = None,
        **kwargs: object,  # noqa: ARG002
    ) -> dict[str, float] | float:
        """Score tool-use compliance.

        Supports two call shapes:

        * **Per row** — how ``mlflow.genai.evaluate`` invokes a scorer: single
          ``outputs`` / ``expectations`` / ``inputs`` values and NO batched
          ``predictions``. Returns a single ``1.0`` / ``0.0``. Previously this
          path fabricated ``{"tool_use/mean": 0.0}`` for every example.
        * **Batch** — CALIBER's own eval path: ``predictions`` + ``inputs``
          lists. Returns ``{"tool_use/mean": float}``.
        """
        # Per-row (MLflow) invocation: no batched predictions, but a single
        # example's outputs/expectations are present.
        if predictions is None and (outputs is not None or expectations is not None):
            example = expectations if isinstance(expectations, dict) else {}
            if "expected_tool" not in example and isinstance(inputs, dict):
                example = inputs
            response = outputs if outputs is not None else {}
            return score_tool_use([example], [response]).overall

        # Batch invocation.
        if inputs is None or predictions is None:
            return {"tool_use/mean": 0.0}
        responses: list[dict[str, Any] | str] = list(predictions)
        result = score_tool_use(list(inputs), responses)
        return {"tool_use/mean": result.overall}
