"""Service-level objectives and alert evaluation over durable run state.

The review recorded these as absent: "alert-rule creation, routing, escalation,
silence, acknowledgement, and history", "SLO/SLI definitions, error budgets, and
burn-rate views", and — under Operations — that CALIBER "stops at observability.
There are useful traces and metrics, but no alert policies, configurable SLOs".

This module closes the *evaluation* half of that, which is the half that has to
exist first: without a declared objective and a computed verdict, routing and
escalation have nothing to route. Deliberately scoped:

**What this is.** Operator-declared objectives, evaluated against the same durable
run state the queue-health and readiness surfaces read, producing a per-objective
verdict with the observed value, the target, and — for ratio objectives — the
remaining error budget and burn rate. Read-only and derived, so it needs no new
schema and cannot disagree with the run records it summarizes.

**What this is not.** Not routing, escalation, silence, acknowledgement, or
incident history; those need a notification subsystem and durable alert state, and
claiming them here would be the same overclaim the review flagged elsewhere. The
signals are also *platform* signals — run success, latency, queue lag, delivery
failure — not per-workflow or per-agent SLOs.

Objectives are declared as a compact comparison list so an operator can define
them without a migration or a CRUD surface::

    CALIBER_SLO_OBJECTIVES="workflow_success_rate>=0.99,workflow_p95_latency_ms<=30000"

An objective naming an unknown signal is reported as a **configuration error**
rather than silently dropped: a declared objective that quietly evaluates nothing
is exactly the "reads as a configured safety control while enforcing nothing"
failure this codebase has been bitten by.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from caliber.db.models import CaliberWorkflowRun

#: Terminal run statuses. ``queued``/``running`` are in flight and belong to the
#: queue-health signal, not to a success ratio.
_SUCCESS_STATUSES = frozenset({"completed"})
_FAILURE_STATUSES = frozenset({"failed", "cancelled", "timed_out"})

#: Signals an objective can name. Each maps to a human description; the value is
#: computed in :func:`collect_signals`.
SIGNALS: dict[str, str] = {
    "workflow_success_rate": "Fraction of terminal workflow runs in the window that completed.",
    "workflow_p95_latency_ms": "95th-percentile end-to-end duration of runs that finished.",
    "workflow_avg_latency_ms": "Mean end-to-end duration of runs that finished.",
    "queue_oldest_wait_seconds": "Age of the oldest run still waiting in the queue.",
    "queue_stale_leases": "Running runs past their lease with no fresh heartbeat.",
    "webhook_dead_letters": "Outbound events that exhausted every delivery attempt.",
    "readiness_blockers": "Required dependency checks currently failing.",
}

#: Signals expressed as a ratio in [0, 1], for which an error budget is meaningful.
_RATIO_SIGNALS = frozenset({"workflow_success_rate"})

_OBJECTIVE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|>|<)\s*([-+0-9.eE]+)\s*$")

_COMPARATORS: dict[str, Any] = {
    ">=": lambda observed, target: observed >= target,
    "<=": lambda observed, target: observed <= target,
    ">": lambda observed, target: observed > target,
    "<": lambda observed, target: observed < target,
}

DEFAULT_WINDOW_MINUTES = 60.0


class ObjectiveConfigError(ValueError):
    """A declared objective could not be parsed or names an unknown signal."""


@dataclass(frozen=True)
class Objective:
    signal: str
    comparator: str
    target: float

    @property
    def label(self) -> str:
        return f"{self.signal}{self.comparator}{self.target:g}"


def parse_objectives(raw: str | None) -> tuple[list[Objective], list[str]]:
    """Parse the declaration list into objectives plus configuration errors.

    Errors are *returned*, not raised: a typo in one objective must not blind the
    operator to the others, and it must still be visible rather than dropped.
    """
    objectives: list[Objective] = []
    errors: list[str] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        match = _OBJECTIVE_RE.match(text)
        if match is None:
            errors.append(
                f"cannot parse objective {text!r}; expected e.g. 'workflow_success_rate>=0.99'"
            )
            continue
        signal, comparator, target = match.groups()
        if signal not in SIGNALS:
            errors.append(
                f"objective {text!r} names unknown signal {signal!r}; "
                "one of: " + ", ".join(sorted(SIGNALS))
            )
            continue
        try:
            objectives.append(Objective(signal=signal, comparator=comparator, target=float(target)))
        except ValueError:
            errors.append(f"objective {text!r} has a non-numeric target")
    return objectives, errors


@dataclass(frozen=True)
class AlertState:
    objective: str
    signal: str
    comparator: str
    target: float
    observed: float | None
    firing: bool
    detail: str
    #: Fraction of the allowed failure budget still unspent, for ratio objectives.
    #: ``None`` for latency/count objectives, where "budget" has no meaning.
    error_budget_remaining: float | None = None
    #: Observed failure rate divided by the allowed failure rate. ``1.0`` means the
    #: budget is being consumed exactly as fast as it is granted; above 1.0 the
    #: objective will be missed if the rate holds.
    burn_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "signal": self.signal,
            "comparator": self.comparator,
            "target": self.target,
            "observed": self.observed,
            "firing": self.firing,
            "detail": self.detail,
            "error_budget_remaining": self.error_budget_remaining,
            "burn_rate": self.burn_rate,
        }


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty sample.

    ``None`` rather than 0.0: an empty window has no latency, and reporting zero
    would make a quiet period look like a perfectly fast one.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(max(int(-(-fraction * len(ordered) // 1)) - 1, 0), len(ordered) - 1)
    return ordered[index]


def collect_signals(
    session: Any,
    *,
    window_minutes: float = DEFAULT_WINDOW_MINUTES,
    queue_health: Any | None = None,
    webhook_delivery: dict[str, Any] | None = None,
    readiness: Any | None = None,
    now: datetime | None = None,
) -> dict[str, float | None]:
    """Compute every signal an objective can name.

    Queue, webhook, and readiness signals are *passed in* from the surfaces that
    already own them, so a single request cannot report one number on the queue
    endpoint and a different one here.

    ``None`` means "no observation in this window" and is distinct from zero — an
    objective reading it does not fire, because an empty window is not evidence of
    a breach.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(window_minutes, 0.0))

    terminal = _SUCCESS_STATUSES | _FAILURE_STATUSES
    rows = (
        session.execute(
            select(
                CaliberWorkflowRun.status,
                CaliberWorkflowRun.started_at,
                CaliberWorkflowRun.completed_at,
            ).where(
                CaliberWorkflowRun.status.in_(tuple(terminal)),
                CaliberWorkflowRun.completed_at.is_not(None),
                CaliberWorkflowRun.completed_at >= cutoff.replace(tzinfo=None),
            )
        )
        .tuples()
        .all()
    )
    succeeded = sum(1 for status, _s, _c in rows if status in _SUCCESS_STATUSES)
    durations: list[float] = []
    for _status, started, completed in rows:
        if started is None or completed is None:
            continue
        durations.append(max((completed - started).total_seconds() * 1000.0, 0.0))

    return {
        "workflow_success_rate": (succeeded / len(rows)) if rows else None,
        "workflow_p95_latency_ms": _percentile(durations, 0.95),
        "workflow_avg_latency_ms": (sum(durations) / len(durations)) if durations else None,
        "queue_oldest_wait_seconds": (
            getattr(queue_health, "oldest_queued_age_seconds", None)
            if queue_health is not None
            else None
        ),
        "queue_stale_leases": (
            float(getattr(queue_health, "stale_leases", 0)) if queue_health is not None else None
        ),
        "webhook_dead_letters": (
            float(webhook_delivery.get("count", 0)) if webhook_delivery is not None else None
        ),
        "readiness_blockers": (
            float(len(getattr(readiness, "blockers", []) or [])) if readiness is not None else None
        ),
    }


def evaluate(objectives: list[Objective], signals: dict[str, float | None]) -> list[AlertState]:
    """Evaluate each objective against the observed signals.

    Ordering follows the declaration order so an operator's list reads back the way
    they wrote it.
    """
    states: list[AlertState] = []
    for objective in objectives:
        observed = signals.get(objective.signal)
        if observed is None:
            states.append(
                AlertState(
                    objective=objective.label,
                    signal=objective.signal,
                    comparator=objective.comparator,
                    target=objective.target,
                    observed=None,
                    firing=False,
                    detail=(
                        f"no {objective.signal} observation in this window; an empty window "
                        "is not evidence of a breach"
                    ),
                )
            )
            continue
        holds = bool(_COMPARATORS[objective.comparator](observed, objective.target))
        budget: float | None = None
        burn: float | None = None
        if objective.signal in _RATIO_SIGNALS and 0.0 <= objective.target < 1.0:
            allowed_failure = 1.0 - objective.target
            observed_failure = max(0.0, 1.0 - observed)
            burn = observed_failure / allowed_failure if allowed_failure else None
            budget = max(0.0, min(1.0, 1.0 - (observed_failure / allowed_failure)))
        states.append(
            AlertState(
                objective=objective.label,
                signal=objective.signal,
                comparator=objective.comparator,
                target=objective.target,
                observed=observed,
                firing=not holds,
                detail=(
                    f"{objective.signal} {observed:.4g} "
                    f"{'meets' if holds else 'violates'} {objective.comparator}{objective.target:g}"
                ),
                error_budget_remaining=budget,
                burn_rate=burn,
            )
        )
    return states


def build_report(
    session: Any,
    *,
    raw_objectives: str | None,
    window_minutes: float = DEFAULT_WINDOW_MINUTES,
    queue_health: Any | None = None,
    webhook_delivery: dict[str, Any] | None = None,
    readiness: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse, evaluate, and package the SLO report for the operations endpoint."""
    objectives, errors = parse_objectives(raw_objectives)
    signals = collect_signals(
        session,
        window_minutes=window_minutes,
        queue_health=queue_health,
        webhook_delivery=webhook_delivery,
        readiness=readiness,
        now=now,
    )
    states = evaluate(objectives, signals)
    return {
        # A configuration error counts as "not healthy": an objective the operator
        # believes is protecting them but which cannot be evaluated is a defect,
        # not a neutral state.
        "healthy": not errors and not any(state.firing for state in states),
        "window_minutes": window_minutes,
        "objectives_configured": len(objectives),
        "configuration_errors": errors,
        "firing": [state.to_dict() for state in states if state.firing],
        "objectives": [state.to_dict() for state in states],
        "signals": signals,
        "available_signals": dict(SIGNALS),
    }


__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "SIGNALS",
    "AlertState",
    "Objective",
    "ObjectiveConfigError",
    "build_report",
    "collect_signals",
    "evaluate",
    "parse_objectives",
]
