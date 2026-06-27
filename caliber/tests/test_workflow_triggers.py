"""Tests for workflow Start triggers: cron matcher, manifest config, the
event-trigger endpoint, and the cron scheduler task."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.orchestrator.scheduler import WorkflowSchedulerTask
from caliber.workflows.cron import cron_matches, next_fires, validate_cron
from caliber.workflows.manifest import parse_manifest
from tests.workflow_helpers import PREFIX, make_manifest

# ---------------------------------------------------------------------------
# Cron matcher
# ---------------------------------------------------------------------------


def test_cron_matches_basic_and_steps() -> None:
    assert cron_matches("* * * * *", datetime(2026, 6, 10, 9, 30))
    assert cron_matches("0 9 * * *", datetime(2026, 6, 10, 9, 0))
    assert not cron_matches("0 9 * * *", datetime(2026, 6, 10, 9, 1))
    assert cron_matches("*/15 * * * *", datetime(2026, 6, 10, 9, 30))
    assert not cron_matches("*/15 * * * *", datetime(2026, 6, 10, 9, 31))
    # 2026-06-08 is a Monday → cron dow 1
    assert cron_matches("0 0 * * 1", datetime(2026, 6, 8, 0, 0))
    # dom OR dow when both restricted
    assert cron_matches("0 0 1 * 5", datetime(2026, 6, 1, 0, 0))
    assert not cron_matches("0 0 1 * 5", datetime(2026, 6, 3, 0, 0))


def test_validate_cron_rejects_garbage() -> None:
    validate_cron("*/5 0-12 1,15 * 1-5")
    for bad in ["* * * *", "60 * * * *", "* 24 * * *", "0 0 0 * *", "x * * * *"]:
        with pytest.raises(ValueError):
            validate_cron(bad)


# ---------------------------------------------------------------------------
# Manifest StartTrigger
# ---------------------------------------------------------------------------


def test_start_trigger_round_trips_and_validates() -> None:
    data = make_manifest("wf")
    data["nodes"]["start"]["trigger"] = {
        "mode": "cron",
        "cron": "*/10 * * * *",
        "timezone": "UTC",
        "alias": "prod",
    }
    manifest = parse_manifest(data)
    assert manifest.nodes["start"].trigger is not None
    assert manifest.nodes["start"].trigger.mode == "cron"

    data["nodes"]["start"]["trigger"] = {"mode": "event"}
    with pytest.raises(Exception, match="event_name"):
        parse_manifest(data)

    data["nodes"]["start"]["trigger"] = {"mode": "cron", "cron": "nope"}
    with pytest.raises(Exception):
        parse_manifest(data)


def test_manifest_without_trigger_is_manual() -> None:
    manifest = parse_manifest(make_manifest("wf"))
    assert manifest.nodes["start"].trigger is None


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_deployed(
    session: Session,
    *,
    workflow_id: str,
    trigger: dict | None,
    alias: str = "prod",
) -> None:
    manifest = make_manifest(workflow_id)
    if trigger is not None:
        manifest["nodes"]["start"]["trigger"] = trigger
    session.add(CaliberWorkflow(workflow_id=workflow_id, name="WF", owner="@t", status="active"))
    session.add(
        CaliberWorkflowVersion(
            version_id=f"{workflow_id}-v1",
            workflow_id=workflow_id,
            version_number=1,
            status="published",
            manifest=manifest,
            manifest_hash="h",
        )
    )
    session.add(
        CaliberWorkflowDeployment(
            deployment_id=f"{workflow_id}-dep",
            workflow_id=workflow_id,
            alias=alias,
            version_id=f"{workflow_id}-v1",
            status="active",
            deployed_at=datetime.now(timezone.utc),
        )
    )
    session.commit()


def _add_deployment_version(
    session: Session,
    *,
    workflow_id: str,
    version_number: int,
    trigger: dict | None,
    alias: str,
) -> None:
    manifest = make_manifest(workflow_id)
    if trigger is not None:
        manifest["nodes"]["start"]["trigger"] = trigger
    version_id = f"{workflow_id}-v{version_number}"
    session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=workflow_id,
            version_number=version_number,
            status="published",
            manifest=manifest,
            manifest_hash=f"h-{version_number}",
        )
    )
    session.add(
        CaliberWorkflowDeployment(
            deployment_id=f"{workflow_id}-{alias}-dep-{version_number}",
            workflow_id=workflow_id,
            alias=alias,
            version_id=version_id,
            status="active",
            deployed_at=datetime.now(timezone.utc),
        )
    )


def _enable_queue(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


# ---------------------------------------------------------------------------
# Event-trigger endpoint
# ---------------------------------------------------------------------------


def test_event_trigger_enqueues_run(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="evt_wf",
            trigger={"mode": "event", "event_name": "order_created", "alias": "prod"},
        )

    r = client.post(f"{PREFIX}/workflows/evt_wf/trigger", json={"event_name": "order_created"})
    assert r.status_code == 202, r.text
    run = r.json()["data"]
    assert run["source"] == "event"
    assert run["deployment_alias"] == "prod"
    assert run["status"] == "queued"


def test_event_trigger_persists_manifest_snapshot_for_replay(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="evt_snapshot",
            trigger={"mode": "event", "event_name": "order_created", "alias": "prod"},
        )

    r = client.post(
        f"{PREFIX}/workflows/evt_snapshot/trigger",
        json={"event_name": "order_created"},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        version = session.get(CaliberWorkflowVersion, "evt_snapshot-v1")
        assert version is not None
        stored_manifest = deepcopy(version.manifest)
        assert run.manifest_snapshot == stored_manifest
        assert run.summary is not None
        assert run.summary["manifest_mode"] == "saved_version"
        assert run.summary["manifest_hash"] == version.manifest_hash
        assert run.summary["workflow_version_number"] == version.version_number
        queued_event = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
            .one()
        )
        assert dict(queued_event.payload or {})["manifest_mode"] == "saved_version"
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["manifest_mode"] == "saved_version"
    assert manifest_data["manifest"] == stored_manifest


def test_event_trigger_uses_configured_alias_when_request_omits_alias(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="evt_staging",
            trigger={"mode": "event", "event_name": "order_created", "alias": "staging"},
            alias="staging",
        )

    r = client.post(f"{PREFIX}/workflows/evt_staging/trigger", json={"event_name": "order_created"})
    assert r.status_code == 202, r.text
    run = r.json()["data"]
    assert run["source"] == "event"
    assert run["deployment_alias"] == "staging"


def test_event_trigger_rejects_non_event_workflow(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        _seed_deployed(session, workflow_id="manual_wf", trigger=None)

    r = client.post(f"{PREFIX}/workflows/manual_wf/trigger", json={})
    assert r.status_code == 409
    assert "not configured for event triggers" in r.json()["detail"]


def test_event_trigger_rejects_event_name_mismatch(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="evt_wf2",
            trigger={"mode": "event", "event_name": "expected", "alias": "prod"},
        )
    r = client.post(f"{PREFIX}/workflows/evt_wf2/trigger", json={"event_name": "other"})
    assert r.status_code == 409
    assert "does not match" in r.json()["detail"]


def test_event_trigger_rejects_alias_mismatch_with_trigger_target(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="evt_mismatch",
            trigger={"mode": "event", "event_name": "expected", "alias": "prod"},
            alias="staging",
        )

    r = client.post(
        f"{PREFIX}/workflows/evt_mismatch/trigger",
        json={"alias": "staging", "event_name": "expected"},
    )
    assert r.status_code == 409
    assert "targets alias 'prod'" in r.json()["detail"]


def test_event_trigger_requires_alias_when_multiple_targets_are_active(client: TestClient) -> None:
    _enable_queue(client)
    with client.app.state.session_factory() as session:
        session.add(
            CaliberWorkflow(workflow_id="evt_multi", name="WF", owner="@t", status="active")
        )
        _add_deployment_version(
            session,
            workflow_id="evt_multi",
            version_number=1,
            trigger={"mode": "event", "event_name": "order_created", "alias": "prod"},
            alias="prod",
        )
        _add_deployment_version(
            session,
            workflow_id="evt_multi",
            version_number=2,
            trigger={"mode": "event", "event_name": "order_created", "alias": "staging"},
            alias="staging",
        )
        session.commit()

    r = client.post(f"{PREFIX}/workflows/evt_multi/trigger", json={"event_name": "order_created"})
    assert r.status_code == 409
    assert "specify alias explicitly" in r.json()["detail"]


def test_event_trigger_requires_queue_enabled(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="evt_wf3",
            trigger={"mode": "event", "event_name": "e", "alias": "prod"},
        )
    r = client.post(f"{PREFIX}/workflows/evt_wf3/trigger", json={})
    assert r.status_code == 409  # queue disabled


# ---------------------------------------------------------------------------
# Cron scheduler
# ---------------------------------------------------------------------------


def test_scheduler_fires_cron_and_dedupes(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="cron_wf",
            trigger={"mode": "cron", "cron": "* * * * *", "timezone": "UTC", "alias": "prod"},
        )

    scheduler = WorkflowSchedulerTask(session_factory=client.app.state.session_factory)
    now = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)

    assert scheduler._tick_inner(now) == 1
    # Same minute → idempotent, no duplicate.
    assert scheduler._tick_inner(now) == 0
    # Next minute → fires again.
    assert scheduler._tick_inner(now.replace(minute=1)) == 1

    with client.app.state.session_factory() as session:
        runs = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_id == "cron_wf")
            .all()
        )
    assert len(runs) == 2
    assert all(r.source == "cron" for r in runs)


def test_scheduler_persists_manifest_snapshot_for_replay(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="cron_snapshot",
            trigger={"mode": "cron", "cron": "* * * * *", "timezone": "UTC", "alias": "prod"},
        )

    scheduler = WorkflowSchedulerTask(session_factory=client.app.state.session_factory)
    now = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    assert scheduler._tick_inner(now) == 1

    with client.app.state.session_factory() as session:
        run = (
            session.query(CaliberWorkflowRun)
            .filter(CaliberWorkflowRun.workflow_id == "cron_snapshot")
            .one()
        )
        version = session.get(CaliberWorkflowVersion, "cron_snapshot-v1")
        assert version is not None
        stored_manifest = deepcopy(version.manifest)
        assert run.manifest_snapshot == stored_manifest
        assert run.summary is not None
        assert run.summary["manifest_mode"] == "saved_version"
        assert run.summary["manifest_hash"] == version.manifest_hash
        assert run.summary["workflow_version_number"] == version.version_number
        queued_event = (
            session.query(CaliberWorkflowRunEvent)
            .filter(CaliberWorkflowRunEvent.workflow_run_id == run.workflow_run_id)
            .one()
        )
        assert dict(queued_event.payload or {})["manifest_mode"] == "saved_version"
        run_id = run.workflow_run_id
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["manifest_mode"] == "saved_version"
    assert manifest_data["manifest"] == stored_manifest


def test_scheduler_ignores_non_matching_and_disabled(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="cron_off",
            trigger={
                "mode": "cron",
                "cron": "0 9 * * *",
                "timezone": "UTC",
                "alias": "prod",
                "enabled": False,
            },
        )
        _seed_deployed(
            session,
            workflow_id="cron_nomatch",
            trigger={"mode": "cron", "cron": "0 9 * * *", "timezone": "UTC", "alias": "prod"},
        )

    scheduler = WorkflowSchedulerTask(session_factory=client.app.state.session_factory)
    # 10:30 — disabled one is off, the 09:00 one doesn't match.
    assert scheduler._tick_inner(datetime(2026, 6, 10, 10, 30, tzinfo=timezone.utc)) == 0


# ---------------------------------------------------------------------------
# Cron next-fire preview (next_fires + the cron-preview endpoint)
# ---------------------------------------------------------------------------


def test_next_fires_every_15_minutes() -> None:
    fires = next_fires("*/15 * * * *", datetime(2026, 6, 10, 9, 0))
    assert fires == [
        datetime(2026, 6, 10, 9, 15),
        datetime(2026, 6, 10, 9, 30),
        datetime(2026, 6, 10, 9, 45),
        datetime(2026, 6, 10, 10, 0),
        datetime(2026, 6, 10, 10, 15),
    ]


def test_next_fires_daily_rolls_to_next_day() -> None:
    fires = next_fires("0 9 * * *", datetime(2026, 6, 10, 10, 0), count=2)
    assert fires == [datetime(2026, 6, 11, 9, 0), datetime(2026, 6, 12, 9, 0)]


def test_next_fires_excludes_the_starting_minute() -> None:
    # A match exactly at ``after`` is excluded; the walk starts at the next minute.
    fires = next_fires("0 9 * * *", datetime(2026, 6, 10, 9, 0), count=1)
    assert fires == [datetime(2026, 6, 11, 9, 0)]


def test_next_fires_impossible_expression_returns_empty() -> None:
    # Feb 30 never occurs; the bounded walk returns [] instead of looping forever.
    assert next_fires("0 0 30 2 *", datetime(2026, 1, 1, 0, 0), limit_minutes=60 * 24 * 40) == []


def test_next_fires_invalid_expression_raises() -> None:
    with pytest.raises(ValueError):
        next_fires("not a cron", datetime(2026, 6, 10, 9, 0))


def test_cron_preview_route_returns_fire_times(client: TestClient) -> None:
    resp = client.get(
        f"{PREFIX}/workflow-cron-preview",
        params={"expr": "*/30 * * * *", "tz": "UTC", "count": "3"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["timezone"] == "UTC"
    assert data["expression"] == "*/30 * * * *"
    assert len(data["fire_times"]) == 3
    minutes = {datetime.fromisoformat(ts).minute for ts in data["fire_times"]}
    assert minutes <= {0, 30}


def test_cron_preview_route_caps_count_at_20(client: TestClient) -> None:
    resp = client.get(
        f"{PREFIX}/workflow-cron-preview",
        params={"expr": "* * * * *", "tz": "UTC", "count": "999"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["fire_times"]) == 20


def test_cron_preview_route_rejects_bad_expression(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/workflow-cron-preview", params={"expr": "99 * * * *", "tz": "UTC"})
    assert resp.status_code == 400


def test_cron_preview_route_rejects_bad_timezone(client: TestClient) -> None:
    resp = client.get(
        f"{PREFIX}/workflow-cron-preview", params={"expr": "0 9 * * *", "tz": "Mars/Phobos"}
    )
    assert resp.status_code == 400
