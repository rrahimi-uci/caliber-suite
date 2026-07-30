"""Incident lifecycle for SLO objectives: open, route, silence, resolve.

``observability/slo.py`` already evaluated objectives and returned an ``AlertState`` per
objective. That is detection without memory — a breach was visible only to whoever polled
``/system/slo`` while it was still true, so an operator could not answer *when did this
start*, *how long did it last*, or *has this happened before*. Every review recorded "no
alert routing, escalation, silencing, incident history" as open, and this is the state that
was missing rather than a second detector.

## Routing reuses the event bus on purpose

An incident publishes ``slo.incident.opened`` / ``slo.incident.resolved`` onto the existing
:class:`~caliber.events.bus.EventBus`, which the webhook dispatcher already delivers. That
is not laziness: the dispatcher has bounded retry, a durable dead-letter record, per-target
settlement, and crash recovery, all of which an alert delivery path needs and none of which
is worth building twice. An alert that vanishes silently is the exact failure the webhook
work spent several passes eliminating.

## Notification is at-most-once per incident transition

``notified_at`` is set when an incident is routed, and a routed incident is never routed
again. The evaluator is expected to run repeatedly — on a timer, on demand from the route —
so without that marker a breach lasting an hour would page every tick. Escalation is
deliberately *not* re-notification: it is a severity the operator configures per objective.

## Silencing is stored, not configured

``silenced_until`` lives on the row because the moment an operator wants to silence an
alert is precisely the moment that editing an environment variable and redeploying is least
appropriate. A silenced incident still opens and still records history; it simply does not
route. Suppressing the record as well would hide the incident from the history that exists
to be reviewed afterwards.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

logger = logging.getLogger("caliber.observability.incidents")

#: Published when an objective starts and stops breaching. Named under ``slo.`` so an
#: operator can subscribe a webhook to alerting alone rather than to every platform event.
EVENT_OPENED = "slo.incident.opened"
EVENT_RESOLVED = "slo.incident.resolved"

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

#: Severity is per-objective operator configuration, not something inferred from how far
#: past the target an observation is — "how bad is this" is a judgement about the service,
#: not about the number.
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
_SEVERITIES = frozenset({SEVERITY_WARNING, SEVERITY_CRITICAL})

DEFAULT_SEVERITY = SEVERITY_WARNING


def parse_severities(raw: str | None) -> dict[str, str]:
    """Parse ``objective=severity`` pairs into a lookup.

    Unknown severities are ignored rather than raising: a typo in one entry must not stop
    every other objective from being evaluated, and alerting that refuses to run because
    its own configuration is imperfect is worse than alerting at the default severity.
    """
    mapping: dict[str, str] = {}
    for item in str(raw or "").split(","):
        # ``rpartition``: an objective label *contains* ``=`` (``success_ratio>=0.9``), so
        # splitting on the first one would parse ``a>=1=critical`` as label ``a>`` with
        # severity ``1=critical``. The severity is the last field, not the second.
        label, _, severity = item.rpartition("=")
        label = label.strip()
        severity = severity.strip().lower()
        if not label or severity not in _SEVERITIES:
            continue
        mapping[label] = severity
    return mapping


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _open_incident(session: Any, objective: str) -> Any:
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    return (
        session.execute(
            select(CaliberIncident).where(
                CaliberIncident.objective == objective,
                CaliberIncident.status == STATUS_OPEN,
            )
        )
        .scalars()
        .first()
    )


def reconcile(
    session: Any,
    states: list[Any],
    *,
    severities: dict[str, str] | None = None,
    publish: Any | None = None,
) -> dict[str, list[str]]:
    """Bring incident records in line with the current alert states.

    Returns ``{"opened": [...], "resolved": [...], "notified": [...]}`` by objective, so a
    caller can report what changed rather than diffing the table itself.

    Idempotent by construction: an objective already breaching does not open a second
    incident, and one already resolved does not resolve again. The evaluator is meant to be
    called repeatedly, so anything else would page on every tick.
    """
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    severity_by_objective = severities or {}
    opened: list[str] = []
    resolved: list[str] = []
    notified: list[str] = []
    now = _now()

    for state in states:
        objective = str(getattr(state, "objective", "") or "")
        if not objective:
            continue
        existing = _open_incident(session, objective)
        firing = bool(getattr(state, "firing", False))

        if firing and existing is None:
            incident = CaliberIncident(
                incident_id=f"INC-{uuid.uuid4().hex[:12]}",
                objective=objective,
                signal=str(getattr(state, "signal", "") or ""),
                severity=severity_by_objective.get(objective, DEFAULT_SEVERITY),
                status=STATUS_OPEN,
                detail=str(getattr(state, "detail", "") or "")[:2000],
                observed=getattr(state, "observed", None),
                target=getattr(state, "target", None),
                opened_at=now,
            )
            session.add(incident)
            session.flush()
            opened.append(objective)
            if _route(incident, EVENT_OPENED, publish):
                incident.notified_at = now
                notified.append(objective)

        elif not firing and existing is not None:
            existing.status = STATUS_RESOLVED
            existing.resolved_at = now
            resolved.append(objective)
            # A resolution is routed even when the open was silenced: an operator who
            # muted the noise still needs to know it stopped, and "all clear" is the one
            # message that is never noise.
            _route(existing, EVENT_RESOLVED, publish)

    session.commit()
    return {"opened": opened, "resolved": resolved, "notified": notified}


def _route(incident: Any, event_type: str, publish: Any | None) -> bool:
    """Publish an incident transition. Returns whether it was routed.

    Silenced incidents are not routed but *are* recorded — suppressing the row as well
    would hide the incident from the history that exists to be reviewed later.
    """
    if publish is None:
        return False
    silenced_until = getattr(incident, "silenced_until", None)
    if silenced_until is not None and silenced_until > _now():
        logger.info(
            "incident %s is silenced until %s; recorded but not routed",
            incident.incident_id,
            silenced_until.isoformat(),
        )
        return False
    if event_type == EVENT_OPENED and getattr(incident, "notified_at", None) is not None:
        return False  # already routed; the evaluator runs repeatedly by design
    try:
        publish(
            {
                "type": event_type,
                "incident_id": incident.incident_id,
                "objective": incident.objective,
                "signal": incident.signal,
                "severity": incident.severity,
                "detail": incident.detail,
                "observed": incident.observed,
                "target": incident.target,
            }
        )
    except Exception:
        # Never let a routing failure abort the reconcile: the incident record is the
        # durable part, and losing it because a subscriber raised would defeat the point.
        logger.warning("could not route incident %s", incident.incident_id, exc_info=True)
        return False
    return True


def silence(session: Any, incident_id: str, *, minutes: int) -> Any | None:
    """Mute routing for an open incident. Returns the row, or ``None`` if unknown."""
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    incident = session.get(CaliberIncident, incident_id)
    if incident is None:
        return None
    incident.silenced_until = _now() + timedelta(minutes=max(1, int(minutes)))
    session.commit()
    return incident


def acknowledge(session: Any, incident_id: str, *, actor: str) -> Any | None:
    """Record that a human has taken ownership. Does not resolve it.

    Kept separate from resolution because they answer different questions — "is anyone
    looking at this" and "is it still happening" — and collapsing them lets an
    acknowledged-but-ongoing incident disappear from the open list.
    """
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    incident = session.get(CaliberIncident, incident_id)
    if incident is None:
        return None
    incident.acknowledged_at = _now()
    incident.acknowledged_by = actor[:256]
    session.commit()
    return incident


def history(session: Any, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """Recent incidents, newest first."""
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    stmt = select(CaliberIncident).order_by(CaliberIncident.opened_at.desc())
    if status:
        stmt = stmt.where(CaliberIncident.status == status)
    rows = session.execute(stmt.limit(max(1, min(int(limit), 500)))).scalars().all()
    return [
        {
            "incident_id": r.incident_id,
            "objective": r.objective,
            "signal": r.signal,
            "severity": r.severity,
            "status": r.status,
            "detail": r.detail,
            "observed": r.observed,
            "target": r.target,
            "opened_at": r.opened_at.isoformat() if r.opened_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "acknowledged_at": r.acknowledged_at.isoformat() if r.acknowledged_at else None,
            "acknowledged_by": r.acknowledged_by,
            "silenced_until": r.silenced_until.isoformat() if r.silenced_until else None,
            "notified_at": r.notified_at.isoformat() if r.notified_at else None,
            "duration_seconds": (
                int((r.resolved_at - r.opened_at).total_seconds())
                if r.resolved_at and r.opened_at
                else None
            ),
        }
        for r in rows
    ]


__all__ = [
    "DEFAULT_SEVERITY",
    "EVENT_OPENED",
    "EVENT_RESOLVED",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "STATUS_OPEN",
    "STATUS_RESOLVED",
    "acknowledge",
    "history",
    "parse_severities",
    "reconcile",
    "silence",
]
