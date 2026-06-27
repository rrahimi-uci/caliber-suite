"""Regression replay run persistence.

Records the eval-replay provenance row that backs a refinement job's
``candidate_ready`` transition (with ``approval_id=None`` — approvals are no
longer created by the eval stage). The row is a durable record of which replay
allowed the candidate to be promoted later via the Apply endpoint.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberRegressionRun,
    CaliberVerificationItem,
)
from caliber.eval.provider import EvalComparison, ScoreSet
from caliber.ids import new_regression_run_id


def candidate_hash(candidate_content: str) -> str:
    """Stable digest for the candidate content a replay evaluated."""
    return hashlib.sha256(candidate_content.encode("utf-8")).hexdigest()


def record_regression_run(
    session: Session,
    *,
    job: CaliberRefinementJob,
    approval: CaliberApprovalRequest | None,
    comparison: EvalComparison,
    gate: Any,
) -> CaliberRegressionRun:
    """Persist the replay result that backs an approval decision.

    ``gate`` is intentionally typed as ``Any`` because the gate module returns
    a small dataclass whose public contract is ``passed``, ``reasons``, and
    ``to_json()``. Keeping that loose avoids coupling this persistence helper
    to the concrete class.
    """
    candidate_content = ""
    if isinstance(job.candidate, dict):
        raw = job.candidate.get("content")
        candidate_content = raw if isinstance(raw, str) else ""
    gate_payload = gate.to_json()
    run = CaliberRegressionRun(
        run_id=new_regression_run_id(),
        job_id=job.job_id,
        approval_id=approval.approval_id if approval is not None else None,
        agent_id=job.agent_id,
        candidate_hash=candidate_hash(candidate_content),
        status="passed" if bool(gate.passed) else "failed",
        required_for_approval=True,
        failure_reason="; ".join(gate.reasons) if not bool(gate.passed) else None,
        dataset_ids=_dataset_ids(comparison),
        trace_sample_ids=_trace_sample_ids(session, job),
        baseline_scores=_score_set_to_json(comparison.baseline),
        candidate_scores=_score_set_to_json(comparison.candidate) or {},
        deltas=dict(comparison.deltas),
        regressions=_regressions(comparison, gate.reasons),
        gate=gate_payload,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(run)
    return run


def _score_set_to_json(scores: ScoreSet | None) -> dict[str, Any] | None:
    if scores is None:
        return None
    return {
        "overall": scores.overall,
        "dimensions": dict(scores.dimensions),
    }


def _dataset_ids(comparison: EvalComparison) -> list[str]:
    if comparison.eval_dataset_id:
        return [comparison.eval_dataset_id]
    return []


def _trace_sample_ids(session: Session, job: CaliberRefinementJob) -> list[str]:
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None:
        return []
    trace_ids: list[str] = []
    if item.trace_id:
        trace_ids.append(item.trace_id)
    context = item.submitted_context or {}
    if isinstance(context, dict):
        raw_trace_id = context.get("trace_id")
        if isinstance(raw_trace_id, str) and raw_trace_id:
            trace_ids.append(raw_trace_id)
        raw_trace_ids = context.get("trace_ids")
        if isinstance(raw_trace_ids, list):
            trace_ids.extend(t for t in raw_trace_ids if isinstance(t, str) and t)
    return list(dict.fromkeys(trace_ids))


def _regressions(comparison: EvalComparison, gate_reasons: list[str]) -> list[dict[str, Any]]:
    regressions = [
        {"metric": metric, "delta": delta}
        for metric, delta in comparison.deltas.items()
        if metric != "overall" and isinstance(delta, int | float) and delta < 0.0
    ]
    if not regressions and gate_reasons:
        regressions.extend({"metric": "gate", "reason": reason} for reason in gate_reasons)
    return regressions
