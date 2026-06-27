"""Evaluation provider + regression gate.

Same dependency-injection shape as :mod:`caliber.llm.provider` and
:mod:`caliber.mlflow_client`: a small Protocol the orchestrator depends on,
a deterministic test double, and a production implementation that wraps the
real backend (``mlflow.genai.evaluate``).

The regression gate (``gate.py``) is a pure function — no side effects, no
dependencies — so the same logic runs whether the eval scores come from a
fake or from a real MLflow eval run.
"""

from __future__ import annotations

from caliber.eval.fake import FakeEvalProvider
from caliber.eval.gate import GateDecision, apply_gate
from caliber.eval.provider import (
    EvalComparison,
    EvalProvider,
    EvalProviderError,
    EvalRequest,
    ScoreSet,
    build_provider,
)

__all__ = [
    "EvalComparison",
    "EvalProvider",
    "EvalProviderError",
    "EvalRequest",
    "FakeEvalProvider",
    "GateDecision",
    "ScoreSet",
    "apply_gate",
    "build_provider",
]
