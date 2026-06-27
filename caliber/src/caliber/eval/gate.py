"""Regression gate — decides whether a candidate is allowed to advance to approval.

Pure function. No side effects, no DB, no MLflow. The orchestrator calls
:func:`apply_gate` with the comparison the provider produced and the
agent's thresholds; the result decides whether an approval request is
created or the job is marked ``rejected``.

The rule:

1. ``min_aggregate_score`` — candidate's ``overall`` must clear this.
2. ``max_regression_delta`` — if a baseline exists, no dimension may
   regress by more than this delta. Cold-start runs (no baseline) skip
   this check.

Defaults match the spec: ``min_aggregate_score=0.85``, ``max_regression_delta=0.02``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from caliber.eval.provider import EvalComparison

DEFAULT_MIN_AGGREGATE_SCORE = 0.85
DEFAULT_MAX_REGRESSION_DELTA = 0.02


@dataclass(frozen=True)
class GateDecision:
    """Output of :func:`apply_gate`.

    ``reasons`` is empty on success and lists every failed rule on rejection
    so the audit trail records *why* a candidate was held back. The list is
    sorted for stable diffs and predictable test assertions.
    """

    passed: bool
    reasons: list[str] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "thresholds": dict(self.thresholds),
        }


def apply_gate(
    comparison: EvalComparison,
    thresholds: dict[str, Any] | None = None,
) -> GateDecision:
    """Evaluate the gate against a candidate/baseline comparison.

    Parameters
    ----------
    comparison:
        Output of :meth:`EvalProvider.evaluate`.
    thresholds:
        ``agent_config.eval_thresholds`` — a JSON dict with optional
        ``min_aggregate_score`` and ``max_regression_delta`` keys.
        Missing keys fall back to the module-level defaults.

    Returns
    -------
    :class:`GateDecision` with the verdict and (on rejection) one
    human-readable reason per violated rule.
    """
    raw = thresholds or {}
    min_overall = float(raw.get("min_aggregate_score", DEFAULT_MIN_AGGREGATE_SCORE))
    max_regression = float(raw.get("max_regression_delta", DEFAULT_MAX_REGRESSION_DELTA))

    reasons: list[str] = []

    overall = comparison.candidate.overall
    if not math.isfinite(overall):
        # Defense-in-depth: a non-finite aggregate (NaN/inf) means the
        # evaluation effectively failed; fail closed rather than letting the
        # comparison below silently evaluate False for NaN.
        reasons.append(f"overall score is non-finite ({overall}); evaluation failed")
    elif overall < min_overall:
        reasons.append(
            f"overall {overall:.3f} below min_aggregate_score {min_overall:.3f}"
        )

    if comparison.baseline is not None:
        for dim, delta in sorted(comparison.deltas.items()):
            if dim == "overall":
                # The overall delta isn't a per-dimension regression — the
                # overall floor above already covers it.
                continue
            if delta < -max_regression:
                reasons.append(
                    f"dimension {dim!r} regressed by {delta:+.3f} "
                    f"(max allowed {-max_regression:+.3f})"
                )

    return GateDecision(
        passed=not reasons,
        reasons=reasons,
        thresholds={
            "min_aggregate_score": min_overall,
            "max_regression_delta": max_regression,
        },
    )
