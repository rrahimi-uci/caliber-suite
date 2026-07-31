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

import pytest

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


def test_concurrent_open_reloads_database_winner_without_duplicate_publish(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second replica can win after this one observes no open row.

    Injecting that commit between the existence read and speculative insert exercises the
    unique-index race deterministically without depending on a thread scheduler. The losing
    savepoint must recover the winner while leaving its outer transaction usable.
    """
    from caliber.db.models import CaliberIncident

    objective = "success_ratio>=0.9"
    published = _Recorder()
    real_open_incident = incidents._open_incident
    calls = 0

    def _miss_then_commit_winner(session: Any, requested: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            with session_factory() as winner:
                winner.add(
                    CaliberIncident(
                        incident_id="INC-concurrent-winner",
                        objective=requested,
                        signal="success_ratio",
                        severity="warning",
                        status="open",
                        detail="opened by another replica",
                        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        notified_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
                winner.commit()
            return None
        return real_open_incident(session, requested)

    monkeypatch.setattr(incidents, "_open_incident", _miss_then_commit_winner)

    with session_factory() as session:
        result = incidents.reconcile(session, [_state(objective, firing=True)], publish=published)
        rows = session.query(CaliberIncident).all()

    assert result == {"opened": [], "resolved": [], "notified": []}
    assert [row.incident_id for row in rows] == ["INC-concurrent-winner"]
    assert published.events == [], "the insert loser must not republish the winner's event"


def test_stale_concurrent_resolver_does_not_publish_twice(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both replicas may read ``open``; only the conditional-update winner publishes."""
    from caliber.db.models import CaliberIncident

    objective = "success_ratio>=0.9"
    with session_factory() as session:
        incidents.reconcile(session, [_state(objective, firing=True)], publish=None)
        stale = session.query(CaliberIncident).one()
        session.expunge(stale)

    published = _Recorder()
    with session_factory() as winner:
        won = incidents.reconcile(winner, [_state(objective, firing=False)], publish=published)

    # This detached object represents a replica that read the row before the winner's
    # commit and reaches its UPDATE afterwards.
    monkeypatch.setattr(incidents, "_open_incident", lambda _session, _objective: stale)
    with session_factory() as loser:
        lost = incidents.reconcile(loser, [_state(objective, firing=False)], publish=published)
        stored = loser.query(CaliberIncident).one()

    assert won["resolved"] == [objective]
    assert lost["resolved"] == []
    assert stored.status == incidents.STATUS_RESOLVED
    assert [event["type"] for event in published.events] == [incidents.EVENT_RESOLVED]


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
    assert rows[0]["resolved_notified_at"] is not None
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
                # The silence is still active. It suppresses another firing notification,
                # but must not suppress the all-clear transition.
                silenced_until=now + timedelta(hours=1),
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
        session.commit()
        row = session.get(CaliberIncident, incident_id)

        assert row.acknowledged_by == "@oncall"
        assert row.acknowledged_at is not None
        assert row.status == "open", "acknowledgement is not resolution"


def test_a_routing_failure_is_persisted_and_retried_on_the_next_tick(session_factory) -> None:
    """The record is the durable part. Losing it because a subscriber raised would defeat
    the purpose of having a history at all."""
    from caliber.db.models import CaliberIncident

    def _explode(_payload: dict[str, Any]) -> None:
        raise RuntimeError("subscriber down")

    objective = "success_ratio>=0.9"
    with session_factory() as session:
        first = incidents.reconcile(
            session, [_state("success_ratio>=0.9", firing=True)], publish=_explode
        )
        stored = session.query(CaliberIncident).all()

    assert first["opened"] == [objective]
    assert first["notified"] == [], "a failed route must not be recorded as notified"
    assert len(stored) == 1
    assert stored[0].notified_at is None

    published = _Recorder()
    with session_factory() as session:
        retried = incidents.reconcile(session, [_state(objective, firing=True)], publish=published)
        notified_at = session.query(CaliberIncident).one().notified_at

    assert retried == {"opened": [], "resolved": [], "notified": [objective]}
    assert [event["type"] for event in published.events] == [incidents.EVENT_OPENED]
    assert notified_at is not None


def test_stale_replica_loses_the_open_notification_claim_without_duplicate_publish(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas can both read ``notified_at = NULL`` before either publishes.

    A detached stale row models the losing read deterministically. The conditional UPDATE,
    rather than the ORM object's old value, must choose the sole publisher.
    """
    from caliber.db.models import CaliberIncident

    objective = "success_ratio>=0.9"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id="INC-notification-race",
                objective=objective,
                signal="success_ratio",
                severity="warning",
                status="open",
                detail="not yet routed",
                opened_at=now,
            )
        )
        session.commit()
        stale = session.query(CaliberIncident).one()
        session.expunge(stale)

    published = _Recorder()
    with session_factory() as winner:
        won = incidents.reconcile(winner, [_state(objective, firing=True)], publish=published)

    monkeypatch.setattr(incidents, "_open_incident", lambda _session, _objective: stale)
    with session_factory() as loser:
        lost = incidents.reconcile(loser, [_state(objective, firing=True)], publish=published)

    assert won["notified"] == [objective]
    assert lost["notified"] == []
    assert [event["type"] for event in published.events] == [incidents.EVENT_OPENED]


def test_a_resolution_publish_failure_is_retried_on_the_next_non_firing_tick(
    session_factory,
) -> None:
    """A durable resolution is not evidence that its all-clear entered the event bus."""
    from caliber.db.models import CaliberIncident

    objective = "success_ratio>=0.9"
    opened = _Recorder()
    with session_factory() as session:
        incidents.reconcile(session, [_state(objective, firing=True)], publish=opened)

    def _fail_resolution(payload: dict[str, Any]) -> None:
        assert payload["type"] == incidents.EVENT_RESOLVED
        raise RuntimeError("event bus unavailable")

    with session_factory() as session:
        first = incidents.reconcile(
            session,
            [_state(objective, firing=False)],
            publish=_fail_resolution,
        )
        failed_marker = session.query(CaliberIncident).one().resolved_notified_at

    assert first["resolved"] == [objective]
    assert failed_marker is None

    retried_events = _Recorder()
    with session_factory() as session:
        retried = incidents.reconcile(
            session,
            [_state(objective, firing=False)],
            publish=retried_events,
        )
        successful_marker = session.query(CaliberIncident).one().resolved_notified_at

    with session_factory() as session:
        steady = incidents.reconcile(
            session,
            [_state(objective, firing=False)],
            publish=retried_events,
        )

    assert retried["resolved"] == []
    assert steady["resolved"] == []
    assert successful_marker is not None
    assert [event["type"] for event in retried_events.events] == [incidents.EVENT_RESOLVED]


def test_stale_replica_loses_the_resolution_notification_claim_without_duplicate_publish(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale pending row cannot bypass the conditional resolution-notification claim."""
    from caliber.db.models import CaliberIncident

    objective = "success_ratio>=0.9"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id="INC-resolution-race",
                objective=objective,
                signal="success_ratio",
                severity="warning",
                status="resolved",
                detail="all-clear not yet routed",
                opened_at=now,
                resolved_at=now,
                notified_at=now,
            )
        )
        session.commit()
        stale = session.query(CaliberIncident).one()
        session.expunge(stale)

    published = _Recorder()
    with session_factory() as winner:
        incidents.reconcile(winner, [_state(objective, firing=False)], publish=published)

    monkeypatch.setattr(incidents, "_pending_resolutions", lambda _session, _objective: [stale])
    with session_factory() as loser:
        incidents.reconcile(loser, [_state(objective, firing=False)], publish=published)
        marker = loser.query(CaliberIncident).one().resolved_notified_at

    assert marker is not None
    assert [event["type"] for event in published.events] == [incidents.EVENT_RESOLVED]


def test_handled_resolution_history_is_never_replayed(session_factory) -> None:
    """Migration-backfilled markers exclude pre-upgrade all-clears from retry queries."""
    from caliber.db.models import CaliberIncident

    objective = "success_ratio>=0.9"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id="INC-historical-resolution",
                objective=objective,
                signal="success_ratio",
                severity="warning",
                status="resolved",
                detail="resolved before marker migration",
                opened_at=now,
                resolved_at=now,
                notified_at=now,
                resolved_notified_at=now,
            )
        )
        session.commit()

    published = _Recorder()
    with session_factory() as session:
        result = incidents.reconcile(
            session,
            [_state(objective, firing=False)],
            publish=published,
        )

    assert result == {"opened": [], "resolved": [], "notified": []}
    assert published.events == []


def test_operator_mutation_helpers_flush_without_owning_the_transaction(session_factory) -> None:
    """A caller can roll back the incident mutation with an adjacent failed audit write."""
    from caliber.db.models import CaliberIncident

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            CaliberIncident(
                incident_id="INC-caller-owned",
                objective="success_ratio>=0.9",
                signal="success_ratio",
                severity="warning",
                status="open",
                detail="transaction boundary",
                opened_at=now,
            )
        )
        session.commit()

    with session_factory() as session:
        assert incidents.silence(session, "INC-caller-owned", minutes=30) is not None
        assert incidents.acknowledge(session, "INC-caller-owned", actor="@oncall") is not None
        session.rollback()

    with session_factory() as session:
        stored = session.get(CaliberIncident, "INC-caller-owned")

    assert stored is not None
    assert stored.silenced_until is None
    assert stored.acknowledged_at is None
    assert stored.acknowledged_by is None


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
