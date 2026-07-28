"""Regression tests for SLO evaluation and the alerts surface.

The review recorded "no alert policies, configurable SLOs, error budgets, or
burn-rate views" and, separately, that CALIBER "stops at observability". These
tests pin the properties that make the new evaluation trustworthy rather than
decorative:

* a declared objective that cannot be evaluated is a reported *configuration
  error*, never a silent pass;
* an empty observation window does not fire (absence of data is not a breach);
* ratio objectives carry a real error budget and burn rate; and
* the endpoint's queue/readiness numbers come from the same collection as the
  dedicated queue endpoint, so the two cannot disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkflowRun
from caliber.observability.queue_health import QueueHealth
from caliber.observability.slo import (
    SIGNALS,
    Objective,
    build_report,
    collect_signals,
    evaluate,
    parse_objectives,
)
from caliber.routes.system_services import ALERTS_PATH

# ---------------------------------------------------------------------------
# Declaration parsing
# ---------------------------------------------------------------------------


def test_objectives_parse_every_comparator() -> None:
    objectives, errors = parse_objectives(
        "workflow_success_rate>=0.99, workflow_p95_latency_ms<=30000,"
        "queue_stale_leases<1, readiness_blockers<1"
    )
    assert errors == []
    assert [o.signal for o in objectives] == [
        "workflow_success_rate",
        "workflow_p95_latency_ms",
        "queue_stale_leases",
        "readiness_blockers",
    ]
    assert [o.comparator for o in objectives] == [">=", "<=", "<", "<"]


def test_an_unknown_signal_is_a_reported_error_not_a_silent_drop() -> None:
    """A declared objective that quietly evaluates nothing is the exact
    "reads as a configured safety control while enforcing nothing" failure."""
    objectives, errors = parse_objectives("workflow_success_rate>=0.99,tone_score>=0.9")
    assert [o.signal for o in objectives] == ["workflow_success_rate"]
    assert len(errors) == 1
    assert "tone_score" in errors[0]
    # And it tells the operator what they can use instead.
    assert "workflow_p95_latency_ms" in errors[0]


def test_a_malformed_objective_does_not_blind_the_others() -> None:
    objectives, errors = parse_objectives("garbage,,workflow_success_rate>=0.5")
    assert [o.signal for o in objectives] == ["workflow_success_rate"]
    assert len(errors) == 1
    assert "cannot parse" in errors[0]


def test_an_empty_declaration_is_not_an_error() -> None:
    assert parse_objectives("") == ([], [])
    assert parse_objectives(None) == ([], [])


# ---------------------------------------------------------------------------
# Evaluation semantics
# ---------------------------------------------------------------------------


def test_a_missing_observation_does_not_fire() -> None:
    """An idle window has no success rate. Firing on that would page an operator
    for a system that is simply quiet."""
    states = evaluate(
        [Objective("workflow_success_rate", ">=", 0.99)],
        {"workflow_success_rate": None},
    )
    assert states[0].firing is False
    assert states[0].observed is None
    assert "not evidence of a breach" in states[0].detail


def test_a_breached_objective_fires_with_the_observed_value() -> None:
    states = evaluate(
        [Objective("workflow_p95_latency_ms", "<=", 1000.0)],
        {"workflow_p95_latency_ms": 4200.0},
    )
    assert states[0].firing is True
    assert states[0].observed == 4200.0
    assert "violates" in states[0].detail


def test_error_budget_and_burn_rate_are_computed_for_a_ratio_objective() -> None:
    """A 99% objective allows 1% failure. Observing 0.5% failure spends half the
    budget at a burn rate of 0.5."""
    states = evaluate(
        [Objective("workflow_success_rate", ">=", 0.99)],
        {"workflow_success_rate": 0.995},
    )
    assert states[0].firing is False
    assert states[0].burn_rate == pytest.approx(0.5)
    assert states[0].error_budget_remaining == pytest.approx(0.5)


def test_an_exhausted_error_budget_is_zero_not_negative() -> None:
    states = evaluate(
        [Objective("workflow_success_rate", ">=", 0.99)],
        {"workflow_success_rate": 0.90},
    )
    assert states[0].firing is True
    assert states[0].error_budget_remaining == 0.0
    assert states[0].burn_rate == pytest.approx(10.0)  # 10% failure vs 1% allowed


def test_latency_objectives_have_no_error_budget() -> None:
    """ "Budget" is meaningful for a ratio; inventing one for a percentile would be
    a number with no definition."""
    states = evaluate(
        [Objective("workflow_p95_latency_ms", "<=", 1000.0)],
        {"workflow_p95_latency_ms": 500.0},
    )
    assert states[0].error_budget_remaining is None
    assert states[0].burn_rate is None


# ---------------------------------------------------------------------------
# Signal collection from durable run state
# ---------------------------------------------------------------------------


def _run(
    session: Session,
    *,
    run_id: str,
    status: str,
    minutes_ago: float,
    duration_seconds: float = 1.0,
) -> None:
    completed = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id="WF-1",
            status=status,
            started_at=(completed - timedelta(seconds=duration_seconds)).replace(tzinfo=None),
            completed_at=completed.replace(tzinfo=None),
        )
    )


def test_success_rate_and_latency_come_from_terminal_runs_in_the_window(
    db_session: Session,
) -> None:
    _run(db_session, run_id="R-1", status="completed", minutes_ago=5, duration_seconds=1)
    _run(db_session, run_id="R-2", status="completed", minutes_ago=5, duration_seconds=2)
    _run(db_session, run_id="R-3", status="failed", minutes_ago=5, duration_seconds=3)
    # Outside the window — must not affect either signal.
    _run(db_session, run_id="R-old", status="failed", minutes_ago=600, duration_seconds=99)
    db_session.commit()

    signals = collect_signals(db_session, window_minutes=60)
    assert signals["workflow_success_rate"] == pytest.approx(2 / 3)
    assert signals["workflow_avg_latency_ms"] == pytest.approx(2000.0)
    assert signals["workflow_p95_latency_ms"] == pytest.approx(3000.0)


def test_in_flight_runs_are_not_counted_as_failures(db_session: Session) -> None:
    """A queued or running job is the queue signal's business. Counting it as a
    failure would make a busy system look like a broken one."""
    _run(db_session, run_id="R-ok", status="completed", minutes_ago=1)
    db_session.add(
        CaliberWorkflowRun(workflow_run_id="R-running", workflow_id="WF-1", status="running")
    )
    db_session.add(
        CaliberWorkflowRun(workflow_run_id="R-queued", workflow_id="WF-1", status="queued")
    )
    db_session.commit()

    assert collect_signals(db_session, window_minutes=60)["workflow_success_rate"] == 1.0


def test_an_empty_window_reports_none_not_zero(db_session: Session) -> None:
    signals = collect_signals(db_session, window_minutes=60)
    assert signals["workflow_success_rate"] is None
    assert signals["workflow_p95_latency_ms"] is None


def test_queue_webhook_and_readiness_signals_are_passed_in(db_session: Session) -> None:
    """They are owned by the surfaces that already collect them, so one request
    cannot report two different queue depths."""
    signals = collect_signals(
        db_session,
        window_minutes=60,
        queue_health=QueueHealth(oldest_queued_age_seconds=412.0, stale_leases=2),
        webhook_delivery={"count": 3},
        readiness=type("R", (), {"blockers": ["database: down"]})(),
    )
    assert signals["queue_oldest_wait_seconds"] == 412.0
    assert signals["queue_stale_leases"] == 2.0
    assert signals["webhook_dead_letters"] == 3.0
    assert signals["readiness_blockers"] == 1.0


def test_a_configuration_error_makes_the_report_unhealthy(db_session: Session) -> None:
    """An objective the operator believes protects them but which cannot be
    evaluated is a defect, not a neutral state."""
    report = build_report(db_session, raw_objectives="nonsense>=1")
    assert report["healthy"] is False
    assert report["configuration_errors"]
    assert report["firing"] == []


def test_no_declared_objectives_is_healthy_and_says_so(db_session: Session) -> None:
    report = build_report(db_session, raw_objectives="")
    assert report["healthy"] is True
    assert report["objectives_configured"] == 0
    assert set(report["available_signals"]) == set(SIGNALS)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_the_alerts_endpoint_reports_a_breached_objective(
    client: TestClient, db_session: Session
) -> None:
    _run(db_session, run_id="R-bad", status="failed", minutes_ago=1)
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"slo_objectives": "workflow_success_rate>=0.99", "slo_window_minutes": 60.0}
    )

    data = client.get(ALERTS_PATH).json()["data"]
    assert data["healthy"] is False
    assert [entry["signal"] for entry in data["firing"]] == ["workflow_success_rate"]
    assert data["firing"][0]["observed"] == 0.0
    assert data["firing"][0]["error_budget_remaining"] == 0.0
    assert data["window_minutes"] == 60.0
    assert "checked_at_ms" in data


def test_the_alerts_endpoint_is_healthy_when_objectives_hold(
    client: TestClient, db_session: Session
) -> None:
    for index in range(5):
        _run(db_session, run_id=f"R-ok-{index}", status="completed", minutes_ago=1)
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"slo_objectives": "workflow_success_rate>=0.99"}
    )

    data = client.get(ALERTS_PATH).json()["data"]
    assert data["healthy"] is True
    assert data["firing"] == []
    assert data["objectives"][0]["observed"] == 1.0


def test_the_alerts_endpoint_requires_a_signed_in_user(client: TestClient) -> None:
    assert client.get(ALERTS_PATH, headers={"X-CALIBER-User": ""}).status_code in (401, 403)
