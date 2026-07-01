"""Version-addressable advisory eval-gate verdicts.

The eval gate is *advisory* in v1: it never blocks an alias rotation, but the
Version panel shows its verdict before a promotion and an operator override is
audited. This module owns the small read/write surface over
:class:`caliber.db.models.CaliberGateVerdict`, used both by the dedicated
``/gate-verdicts`` routes and by the audited prompt promote (which stamps the
operator-supplied verdict so the panel and the timeline stay consistent).

``state`` is the authoritative verdict, computed upstream from BOTH gate rules
(the aggregate floor AND the per-dimension regression rule); the numeric columns
are display detail. There is one row per ``(artifact_type, version_key)`` — it
is upserted so it always reflects the most recent evaluation of that version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberGateVerdict
from caliber.ids import new_gate_verdict_id

# Allowed verdict states (mirrors the FE ``GateVerdict.state`` union). ``none``
# means "no comparison exists for this version" — rendered honestly, never faked
# as a pass.
GATE_STATES: frozenset[str] = frozenset({"pass", "fail", "none", "pending", "stale"})

_NUMERIC_FIELDS = (
    "score",
    "baseline_score",
    "min_aggregate_score",
    "worst_regression",
    "max_regression_delta",
)


def get_gate_verdict(
    session: Session, artifact_type: str, version_key: str
) -> CaliberGateVerdict | None:
    """Return the persisted verdict for one artifact version, or ``None``."""
    return (
        session.execute(
            select(CaliberGateVerdict)
            .where(CaliberGateVerdict.artifact_type == artifact_type)
            .where(CaliberGateVerdict.version_key == version_key)
        )
        .scalars()
        .first()
    )


def record_gate_verdict(
    session: Session,
    *,
    artifact_type: str,
    version_key: str,
    state: str,
    score: float | None = None,
    baseline_score: float | None = None,
    min_aggregate_score: float | None = None,
    worst_regression: float | None = None,
    max_regression_delta: float | None = None,
    eval_run_id: str | None = None,
    evaluated_at: datetime | None = None,
) -> CaliberGateVerdict:
    """Upsert the verdict for ``(artifact_type, version_key)``.

    Does not commit — the caller commits in the same transaction as its own
    state change (matching the audit-record convention).
    """
    if state not in GATE_STATES:
        raise ValueError(f"invalid gate state {state!r}; expected one of {sorted(GATE_STATES)}")

    row = get_gate_verdict(session, artifact_type, version_key)
    if row is None:
        row = CaliberGateVerdict(
            gate_verdict_id=new_gate_verdict_id(),
            artifact_type=artifact_type,
            version_key=version_key,
            state=state,
        )
        try:
            # Isolate the insert in a savepoint: two concurrent evaluations of
            # the same version both read ``None`` above and race to insert,
            # violating ``uq_gate_verdict_artifact_version``. Rather than fail
            # the caller's whole transaction (e.g. a prompt promote) on an
            # advisory verdict, fall back to updating the row the winner
            # committed. The savepoint keeps the caller's prior work intact.
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            row = get_gate_verdict(session, artifact_type, version_key)
            if row is None:  # pragma: no cover - constraint fired without a visible row
                raise
    row.state = state
    row.score = score
    row.baseline_score = baseline_score
    row.min_aggregate_score = min_aggregate_score
    row.worst_regression = worst_regression
    row.max_regression_delta = max_regression_delta
    row.eval_run_id = eval_run_id
    row.evaluated_at = evaluated_at
    return row


def serialize_gate_verdict(row: CaliberGateVerdict | None) -> dict[str, Any]:
    """Serialize a verdict row to the FE ``GateVerdict`` shape.

    A missing row serializes to ``state: "none"`` so the panel renders honestly
    (no verdict for this version) rather than blanking or faking a pass.
    """
    if row is None:
        return {"state": "none"}
    return {
        "state": row.state,
        "score": row.score,
        "baseline_score": row.baseline_score,
        "min_aggregate_score": row.min_aggregate_score,
        "worst_regression": row.worst_regression,
        "max_regression_delta": row.max_regression_delta,
        "eval_run_id": row.eval_run_id,
        "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
    }
