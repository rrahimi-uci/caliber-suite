"""Route tests for ``GET /caliber/system/queue``.

Closes the operations gap recorded in ``product-complete-report.md`` §6:
"queue-depth/worker operations" and "worker liveness, queue lag" had no surface,
so the only liveness signal was a database ``SELECT 1``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkflowRun
from caliber.routes.system_services import QUEUE_PATH


def _run(
    session: Session,
    run_id: str,
    status: str,
    *,
    queued_at: datetime | None = None,
    claimed_by: str | None = None,
    heartbeat: datetime | None = None,
) -> None:
    session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id="WF-1",
            workflow_version_id="WFV-1",
            status=status,
            queued_at=queued_at or datetime.now(timezone.utc),
            claimed_by=claimed_by,
            last_heartbeat_at=heartbeat,
        )
    )
    session.commit()


def test_queue_endpoint_reports_healthy_when_idle(client: TestClient) -> None:
    resp = client.get(QUEUE_PATH)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["healthy"] is True
    assert data["queued"] == 0 and data["running"] == 0
    assert data["degraded_reasons"] == []
    # The thresholds that produced the verdict are disclosed, so an operator can
    # tell a tolerance change from a real backlog.
    assert data["lease_seconds"] > 0
    assert data["max_queue_age_seconds"] > 0
    assert isinstance(data["checked_at_ms"], int)


def test_queue_endpoint_reports_depth_and_live_worker(
    client: TestClient, db_session: Session
) -> None:
    now = datetime.now(timezone.utc)
    _run(db_session, "R-q", "queued", queued_at=now)
    _run(db_session, "R-r", "running", claimed_by="worker-1", heartbeat=now)

    data = client.get(QUEUE_PATH).json()["data"]

    assert data["queued"] == 1
    assert data["running"] == 1
    assert data["workers_alive"] == 1
    assert data["healthy"] is True


def test_queue_endpoint_flags_a_backlog_with_no_live_worker(
    client: TestClient, db_session: Session
) -> None:
    """The case the review named: nothing is consuming the queue."""
    _run(
        db_session,
        "R-stuck",
        "queued",
        queued_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    data = client.get(QUEUE_PATH).json()["data"]

    assert data["healthy"] is False
    assert data["queued"] == 1
    assert data["workers_alive"] == 0
    assert data["oldest_queued_age_seconds"] > 3600
    assert any("no live worker" in r for r in data["degraded_reasons"])


def test_queue_endpoint_flags_an_abandoned_run(client: TestClient, db_session: Session) -> None:
    """A claimed run whose heartbeat stopped: the worker died mid-run."""
    _run(
        db_session,
        "R-abandoned",
        "running",
        claimed_by="worker-1",
        heartbeat=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    data = client.get(QUEUE_PATH).json()["data"]

    assert data["healthy"] is False
    assert data["stale_leases"] == 1
    assert data["workers_alive"] == 0
    assert any("past lease" in r for r in data["degraded_reasons"])


def test_queue_endpoint_requires_authentication(client: TestClient) -> None:
    resp = client.get(QUEUE_PATH, headers={"X-CALIBER-User": ""})
    assert resp.status_code == 401, resp.text


def test_queue_tolerance_is_configurable(client: TestClient, db_session: Session) -> None:
    """A backlog inside the configured tolerance must not report degraded."""
    _run(
        db_session,
        "R-recent",
        "queued",
        queued_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    _run(db_session, "R-r", "running", claimed_by="w1", heartbeat=datetime.now(timezone.utc))

    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_queue_max_age_seconds": 3600.0}
    )
    assert client.get(QUEUE_PATH).json()["data"]["healthy"] is True

    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_queue_max_age_seconds": 1.0}
    )
    tight = client.get(QUEUE_PATH).json()["data"]
    assert tight["healthy"] is False
    assert tight["max_queue_age_seconds"] == 1.0
