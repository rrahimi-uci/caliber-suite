"""Incident lifecycle: the memory that SLO detection lacked.

``observability/slo.py`` already evaluated objectives and returned an ``AlertState`` per
objective, so this is not a second detector — it is the state that turns a stateless gauge
into something an operator can ask questions of. Every review recorded "no alert routing,
escalation, silencing, incident history" as open; the missing piece was persistence and
delivery, not evaluation.

The assertions worth having are the ones about *repetition and suppression*, because those
are what make alerting usable rather than merely present:

- a breach that persists across evaluations must not page on every tick;
- a silenced incident must still be recorded, or the history it exists for is incomplete;
- a resolution must route even when the open was silenced, because "all clear" is the one
  message that is never noise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from caliber.observability import incidents
from caliber.observability.slo import AlertState


def _state(objective: str, *, firing: bool, observed: float = 0.5) -> AlertState:
    return AlertState(
        objective=objective,
        signal="success_ratio",
        comparator=">=",
        target=0.9,
        observed=observed,
        firing=firing,
        detail=f"{objective} observed {observed}",
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


def test_a_breach_opens_one_incident_and_routes_it_once(session_factory) -> None:
    """The evaluator runs repeatedly by design, so re-notifying every tick would page an
    operator continuously for a single ongoing problem."""
    published = _Recorder()
    states = [_state("success_ratio>=0.9", firing=True)]

    with session_factory() as session:
        first = incidents.reconcile(session, states, publish=published)
    with session_factory() as session:
        second = incidents.reconcile(session, states, publish=published)

    assert first["opened"] == ["success_ratio>=0.9"]
    assert first["notified"] == ["success_ratio>=0.9"]
    # Still breaching, so no second incident and no second page.
    assert second["opened"] == []
    assert second["notified"] == []
    assert [e["type"] for e in published.events] == [incidents.EVENT_OPENED]


def test_recovery_resolves_the_incident_and_records_a_duration(session_factory) -> None:
    published = _Recorder()
    objective = "success_ratio>=0.9"

    with session_factory() as session:
        incidents.reconcile(session, [_state(objective, firing=True)], publish=published)
    with session_factory() as session:
        result = incidents.reconcile(session, [_state(objective, firing=False)], publish=published)
        rows = incidents.history(session)

    assert result["resolved"] == [objective]
    assert [e["type"] for e in published.events] == [
        incidents.EVENT_OPENED,
        incidents.EVENT_RESOLVED,
    ]
    assert rows[0]["status"] == "resolved"
    assert rows[0]["duration_seconds"] is not None


def test_a_silenced_incident_is_still_recorded(session_factory) -> None:
    """Silencing suppresses *routing*, not history.

    Dropping the row as well would hide the incident from exactly the record an operator
    reviews afterwards — and the reason they silenced it is usually that they already know.
    """
    from caliber.db.models import CaliberIncident

    published = _Recorder()
    objective = "success_ratio>=0.9"

    with session_factory() as session:
        incidents.reconcile(session, [_state(objective, firing=True)], publish=published)
        opened = session.query(CaliberIncident).one()
        incident_id = opened.incident_id
        # Silence it, then resolve and re-open to exercise the suppressed path.
        incidents.silence(session, incident_id, minutes=60)
        incidents.reconcile(session, [_state(objective, firing=False)], publish=published)
    published.events.clear()

    with session_factory() as session:
        session.query(CaliberIncident).delete()
        session.add(
            CaliberIncident(
                incident_id="INC-silenced",
                objective=objective,
                signal="success_ratio",
                severity="warning",
                status="open",
                detail="pre-silenced",
                opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
                silenced_until=(
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
                ),
            )
        )
        session.commit()
        # Already open and silenced: a further breach must neither duplicate nor route.
        result = incidents.reconcile(session, [_state(objective, firing=True)], publish=published)
        rows = incidents.history(session)

    assert result["opened"] == []
    assert published.events == [], "a silenced incident must not route"
    assert any(r["incident_id"] == "INC-silenced" for r in rows), "but it must stay in history"


def test_a_resolution_routes_even_when_the_open_was_silenced(session_factory) -> None:
    """ "All clear" is the one message that is never noise, so it is not suppressed."""
    from caliber.db.models import CaliberIncident

    published = _Recorder()
    objective = "success_ratio>=0.9"
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id="INC-quiet",
                objective=objective,
                signal="success_ratio",
                severity="warning",
                status="open",
                detail="silenced while ongoing",
                opened_at=now,
                # Silence already expired, so the resolution routes normally.
                silenced_until=now - timedelta(minutes=1),
            )
        )
        session.commit()
        incidents.reconcile(session, [_state(objective, firing=False)], publish=published)

    assert [e["type"] for e in published.events] == [incidents.EVENT_RESOLVED]


def test_acknowledging_does_not_resolve(session_factory) -> None:
    """ "Someone is looking at this" and "it stopped" are different facts. Merging them
    would drop an ongoing incident off the open list while it is still happening."""
    from caliber.db.models import CaliberIncident

    with session_factory() as session:
        incidents.reconcile(session, [_state("success_ratio>=0.9", firing=True)], publish=None)
        incident_id = session.query(CaliberIncident).one().incident_id
        incidents.acknowledge(session, incident_id, actor="@oncall")
        row = session.get(CaliberIncident, incident_id)

        assert row.acknowledged_by == "@oncall"
        assert row.acknowledged_at is not None
        assert row.status == "open", "acknowledgement is not resolution"


def test_a_routing_failure_does_not_lose_the_incident(session_factory) -> None:
    """The record is the durable part. Losing it because a subscriber raised would defeat
    the purpose of having a history at all."""
    from caliber.db.models import CaliberIncident

    def _explode(_payload: dict[str, Any]) -> None:
        raise RuntimeError("subscriber down")

    with session_factory() as session:
        result = incidents.reconcile(
            session, [_state("success_ratio>=0.9", firing=True)], publish=_explode
        )
        stored = session.query(CaliberIncident).all()

    assert result["opened"] == ["success_ratio>=0.9"]
    assert result["notified"] == [], "a failed route must not be recorded as notified"
    assert len(stored) == 1


def test_severity_is_operator_configuration_not_inferred() -> None:
    """How bad a breach is depends on the service, not on how far past the target the
    number happens to be."""
    parsed = incidents.parse_severities("a>=1=critical, b>=2=warning, c>=3=bogus, =critical")

    assert parsed == {"a>=1": "critical", "b>=2": "warning"}
    # A typo in one entry must not stop the others being evaluated.
    assert "c>=3" not in parsed


def test_history_can_be_filtered_by_status(session_factory) -> None:
    published = _Recorder()
    with session_factory() as session:
        incidents.reconcile(session, [_state("a>=1", firing=True)], publish=published)
        incidents.reconcile(
            session,
            [_state("a>=1", firing=False), _state("b>=2", firing=True)],
            publish=published,
        )
        open_only = incidents.history(session, status="open")
        resolved_only = incidents.history(session, status="resolved")

    assert [r["objective"] for r in open_only] == ["b>=2"]
    assert [r["objective"] for r in resolved_only] == ["a>=1"]
