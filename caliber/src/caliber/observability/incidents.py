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

## The database arbitrates each local transition

The partial unique index on open incidents chooses one opener when replicas observe a breach
at the same time, and resolution is a conditional ``open -> resolved`` update. Publication
has its own database arbitration for both transitions: conditional ``NULL -> timestamp``
updates on ``notified_at`` and ``resolved_notified_at`` are the claims, and only their
winners publish. A known publication failure releases its claim before the reconciliation
transaction commits, so a later evaluator tick retries it.

This is not exactly-once external delivery: publishing and committing the incident row
cannot be one atomic transaction with an arbitrary external subscriber. A process failure
after local publication but before reconciliation commits can still produce a duplicate
retry; the durable webhook delivery layer provides its own retry and settlement semantics
after local publication.

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

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

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


def _pending_resolutions(session: Any, objective: str) -> list[Any]:
    """Return unresolved all-clear publications for one objective, oldest first."""
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    return list(
        session.execute(
            select(CaliberIncident)
            .where(
                CaliberIncident.objective == objective,
                CaliberIncident.status == STATUS_RESOLVED,
                CaliberIncident.resolved_notified_at.is_(None),
            )
            .order_by(CaliberIncident.opened_at.asc(), CaliberIncident.incident_id.asc())
        )
        .scalars()
        .all()
    )


def _claim_and_route_open(
    session: Any,
    incident: Any,
    *,
    publish: Any | None,
    claimed_at: datetime,
) -> bool:
    """Claim and publish one open notification in the reconciliation transaction.

    ``notified_at`` is both the durable success marker and the conditional database claim.
    The UPDATE remains uncommitted while the synchronous local event-bus publication runs,
    so another replica either blocks and then loses the predicate or observes the committed
    marker. A process exit rolls the claim back automatically; a known publication failure
    explicitly restores ``NULL`` so the next evaluation can retry.

    The event bus and database are not one transactional resource. If publication succeeds
    and the later database commit fails, the next evaluation can publish a duplicate. That
    at-least-once crash boundary is preferable to permanently marking an event delivered
    before it entered the bus, and downstream webhook delivery has its own durable IDs and
    settlement semantics.
    """
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    if publish is None:
        return False

    claimed = session.execute(
        update(CaliberIncident)
        .where(
            CaliberIncident.incident_id == incident.incident_id,
            CaliberIncident.status == STATUS_OPEN,
            CaliberIncident.notified_at.is_(None),
        )
        .values(notified_at=claimed_at)
    )
    if int(getattr(claimed, "rowcount", 0) or 0) != 1:
        return False

    if _route(incident, EVENT_OPENED, publish):
        return True

    # The claim and its release share the same reconciliation transaction. Matching the
    # claim value prevents this cleanup from erasing a marker if the callback itself
    # changed the row through unusual application code.
    session.execute(
        update(CaliberIncident)
        .where(
            CaliberIncident.incident_id == incident.incident_id,
            CaliberIncident.status == STATUS_OPEN,
            CaliberIncident.notified_at == claimed_at,
        )
        .values(notified_at=None)
    )
    return False


def _claim_and_route_resolution(
    session: Any,
    incident: Any,
    *,
    publish: Any | None,
    claimed_at: datetime,
) -> bool:
    """Claim and publish one resolution in the reconciliation transaction.

    This mirrors :func:`_claim_and_route_open` with an independent marker. Keeping the
    markers separate is essential: an incident can have a successfully published opening
    and a failed, still-pending all-clear. The uncommitted conditional update serializes
    live replicas; rollback handles process loss, while a known failure explicitly restores
    ``NULL`` for the next non-firing reconciliation.
    """
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    if publish is None:
        return False

    claimed = session.execute(
        update(CaliberIncident)
        .where(
            CaliberIncident.incident_id == incident.incident_id,
            CaliberIncident.status == STATUS_RESOLVED,
            CaliberIncident.resolved_notified_at.is_(None),
        )
        .values(resolved_notified_at=claimed_at)
    )
    if int(getattr(claimed, "rowcount", 0) or 0) != 1:
        return False

    if _route(incident, EVENT_RESOLVED, publish):
        return True

    session.execute(
        update(CaliberIncident)
        .where(
            CaliberIncident.incident_id == incident.incident_id,
            CaliberIncident.status == STATUS_RESOLVED,
            CaliberIncident.resolved_notified_at == claimed_at,
        )
        .values(resolved_notified_at=None)
    )
    return False


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

    Idempotent under concurrent replicas: conditional database updates choose each
    transition and notification winner, while the partial unique index chooses one opener.
    The evaluator is meant to be called repeatedly, so anything else would page on every
    tick.

    Reconciliation owns its transaction because a successful local publication and its
    ``notified_at`` marker must settle together before another evaluator can claim it.
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

        if firing:
            if existing is None:
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
                try:
                    # Both replicas can miss the existence query above. Isolate the
                    # speculative insert in a savepoint so the partial unique index can
                    # pick the winner without poisoning the caller's outer transaction.
                    with session.begin_nested():
                        session.add(incident)
                        session.flush()
                except IntegrityError:
                    # The winner committed while this replica was inserting. Reload it so
                    # this tick can retry a failed notification, but the insert loser must
                    # not report a second open transition.
                    existing = _open_incident(session, objective)
                    if existing is None:  # pragma: no cover - constraint fired without winner
                        raise
                else:
                    existing = incident
                    opened.append(objective)

            if existing is not None and _claim_and_route_open(
                session,
                existing,
                publish=publish,
                claimed_at=now,
            ):
                notified.append(objective)

        else:
            if existing is not None:
                transitioned = session.execute(
                    update(CaliberIncident)
                    .where(
                        CaliberIncident.incident_id == existing.incident_id,
                        CaliberIncident.status == STATUS_OPEN,
                    )
                    .values(
                        status=STATUS_RESOLVED,
                        resolved_at=now,
                        resolved_notified_at=None,
                    )
                )
                if int(getattr(transitioned, "rowcount", 0) or 0) == 1:
                    resolved.append(objective)

            # Resolve and publication have separate database claims. This evaluator may
            # publish the transition it just won, retry one whose earlier publisher
            # failed, or lose to a replica that already claimed it. Query all pending
            # history so a second breach/recovery cycle cannot strand an older all-clear.
            for pending in _pending_resolutions(session, objective):
                _claim_and_route_resolution(
                    session,
                    pending,
                    publish=publish,
                    claimed_at=now,
                )

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
    # Silences suppress only the noisy, repeated failure transition. A resolution is
    # deliberately always routed: an operator who muted an ongoing incident still needs
    # the all-clear, and the reconcile() contract has always promised that behaviour.
    if event_type == EVENT_OPENED and silenced_until is not None and silenced_until > _now():
        logger.info(
            "incident %s is silenced until %s; recorded but not routed",
            incident.incident_id,
            silenced_until.isoformat(),
        )
        return False
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
    """Mute routing for an open incident. Flushes; the caller owns the commit."""
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    incident = session.get(CaliberIncident, incident_id)
    if incident is None:
        return None
    incident.silenced_until = _now() + timedelta(minutes=max(1, int(minutes)))
    session.flush()
    return incident


def acknowledge(session: Any, incident_id: str, *, actor: str) -> Any | None:
    """Record that a human has taken ownership. Does not resolve it.

    Kept separate from resolution because they answer different questions — "is anyone
    looking at this" and "is it still happening" — and collapsing them lets an
    acknowledged-but-ongoing incident disappear from the open list. Flushes but does not
    commit so a caller can include its audit row in the same transaction.
    """
    from caliber.db.models import CaliberIncident  # noqa: PLC0415

    incident = session.get(CaliberIncident, incident_id)
    if incident is None:
        return None
    incident.acknowledged_at = _now()
    incident.acknowledged_by = actor[:256]
    session.flush()
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
            "resolved_notified_at": (
                r.resolved_notified_at.isoformat() if r.resolved_notified_at else None
            ),
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
