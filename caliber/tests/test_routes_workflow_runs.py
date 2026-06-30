"""Tests for workflow-run indexing + compiled bundle (plan §11.2, §13.3, §14.5)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkflowRun
from caliber.ids import new_workflow_run_id
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
)


def _support_version(client: TestClient) -> tuple[str, str]:
    register_demo_tools(client)
    wid = create_workflow(client, "Runs WF")
    vid, _ = create_draft(client, wid, make_support_manifest(wid))
    return wid, vid


def test_preview_records_a_workflow_run(client: TestClient) -> None:
    wid, vid = _support_version(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/preview-run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["workflow_run_id"].startswith("WR-")

    runs = client.get(f"{PREFIX}/workflows/{wid}/runs").json()["data"]
    assert len(runs) == 1
    run = runs[0]
    assert run["workflow_version_id"] == vid
    assert run["summary"]["preview"] is True
    assert "support_agent" in run["summary"]["node_path"]


def test_runs_list_empty(client: TestClient) -> None:
    wid, _ = _support_version(client)
    assert client.get(f"{PREFIX}/workflows/{wid}/runs").json()["data"] == []


def test_runs_list_supports_search_filter_and_pagination(
    client: TestClient, db_session: Session
) -> None:
    wid, vid = _support_version(client)
    now = datetime.now(timezone.utc)
    runs = [
        CaliberWorkflowRun(
            workflow_run_id="WR-newest",
            workflow_id=wid,
            workflow_version_id=vid,
            trace_id="trace-newest",
            status="completed",
            queued_at=now,
            summary={"node_path": ["support_agent"]},
        ),
        CaliberWorkflowRun(
            workflow_run_id="WR-middle",
            workflow_id=wid,
            workflow_version_id=vid,
            trace_id="trace-middle",
            status="completed",
            queued_at=now - timedelta(minutes=1),
            summary={
                "artifact_persistence": {
                    "status": "persisted",
                    "bucket": "workflow-artifacts",
                    "artifact_names": ["transcript.json"],
                }
            },
        ),
        CaliberWorkflowRun(
            workflow_run_id="WR-oldest",
            workflow_id=wid,
            workflow_version_id=vid,
            trace_id="trace-oldest",
            status="completed",
            queued_at=now - timedelta(minutes=2),
            summary={
                "artifact_persistence": {
                    "status": "failed",
                    "bucket": "workflow-artifacts",
                    "artifact_names": ["trace-oldest.json"],
                    "error": "object store offline",
                    "failed_object_key": "pipeline/WR-oldest/trace-oldest.json",
                    "recent_persisted_keys": ["pipeline/WR-oldest/report.html"],
                }
            },
        ),
    ]
    db_session.add_all(runs)
    db_session.commit()

    first_page = client.get(f"{PREFIX}/workflows/{wid}/runs?limit=2")
    assert first_page.status_code == 200
    assert [row["workflow_run_id"] for row in first_page.json()["data"]] == [
        "WR-newest",
        "WR-middle",
    ]
    assert first_page.json()["next_cursor"] == "2"

    second_page = client.get(
        f"{PREFIX}/workflows/{wid}/runs?limit=2&cursor={first_page.json()['next_cursor']}"
    )
    assert second_page.status_code == 200
    assert [row["workflow_run_id"] for row in second_page.json()["data"]] == [
        "WR-oldest",
    ]
    assert second_page.json()["next_cursor"] is None

    filtered = client.get(
        f"{PREFIX}/workflows/{wid}/runs"
        "?limit=1&artifact_persistence=failed&search=object%20store%20offline"
    )
    assert filtered.status_code == 200
    assert [row["workflow_run_id"] for row in filtered.json()["data"]] == ["WR-oldest"]
    assert filtered.json()["next_cursor"] is None

    filtered_by_key = client.get(
        f"{PREFIX}/workflows/{wid}/runs"
        "?artifact_persistence=failed&search=pipeline%2FWR-oldest%2Ftrace-oldest.json"
    )
    assert filtered_by_key.status_code == 200
    assert [row["workflow_run_id"] for row in filtered_by_key.json()["data"]] == [
        "WR-oldest",
    ]


def test_runs_stats_reports_exact_counts_and_filtered_matches(
    client: TestClient, db_session: Session
) -> None:
    wid, vid = _support_version(client)
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            CaliberWorkflowRun(
                workflow_run_id="WR-waiting",
                workflow_id=wid,
                workflow_version_id=vid,
                trace_id="trace-waiting",
                status="waiting_event",
                queued_at=now,
                summary={"node_path": ["support_agent"]},
            ),
            CaliberWorkflowRun(
                workflow_run_id="WR-persisted",
                workflow_id=wid,
                workflow_version_id=vid,
                trace_id="trace-persisted",
                status="completed",
                queued_at=now - timedelta(minutes=1),
                summary={
                    "artifact_persistence": {
                        "status": "persisted",
                        "bucket": "workflow-artifacts",
                        "artifact_names": ["transcript.json"],
                    }
                },
            ),
            CaliberWorkflowRun(
                workflow_run_id="WR-failed",
                workflow_id=wid,
                workflow_version_id=vid,
                trace_id="trace-failed",
                status="completed",
                queued_at=now - timedelta(minutes=2),
                summary={
                    "artifact_persistence": {
                        "status": "failed",
                        "bucket": "workflow-artifacts",
                        "artifact_names": ["failed.json"],
                        "error": "object store offline",
                    }
                },
            ),
        ]
    )
    db_session.commit()

    stats = client.get(f"{PREFIX}/workflows/{wid}/runs/stats")
    assert stats.status_code == 200
    assert stats.json()["data"] == {
        "workflow_id": wid,
        "total_runs": 3,
        "matching_runs": 3,
        "waiting_event_runs": 1,
        "artifact_persistence": {
            "failed": 1,
            "persisted": 1,
        },
    }

    filtered = client.get(
        f"{PREFIX}/workflows/{wid}/runs/stats"
        "?artifact_persistence=failed&search=object%20store%20offline"
    )
    assert filtered.status_code == 200
    assert filtered.json()["data"] == {
        "workflow_id": wid,
        "total_runs": 3,
        "matching_runs": 1,
        "waiting_event_runs": 1,
        "artifact_persistence": {
            "failed": 1,
            "persisted": 1,
        },
    }


def test_run_by_trace_resolves(client: TestClient, db_session: Session) -> None:
    wid, vid = _support_version(client)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=new_workflow_run_id(),
            workflow_id=wid,
            workflow_version_id=vid,
            deployment_alias="prod",
            trace_id="trace-xyz",
            status="completed",
            summary={"node_path": ["support_agent"]},
        )
    )
    db_session.commit()
    r = client.get(f"{PREFIX}/workflow-runs/by-trace/trace-xyz")
    assert r.status_code == 200
    assert r.json()["data"]["workflow_version_id"] == vid


def test_run_by_trace_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/workflow-runs/by-trace/missing").status_code == 404


def _seed_run(client: TestClient, db_session: Session, *, trace_id: str | None) -> str:
    wid, vid = _support_version(client)
    run_id = new_workflow_run_id()
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id=wid,
            workflow_version_id=vid,
            trace_id=trace_id,
            status="completed",
            summary={"node_path": ["support_agent"]},
        )
    )
    db_session.commit()
    return run_id


def test_run_trace_empty_when_no_trace_id(client: TestClient, db_session: Session) -> None:
    # Default fake-provider / tracing-off path: the run has no trace_id, so the
    # endpoint returns 200 with an empty span list (friendly empty state), not 404.
    run_id = _seed_run(client, db_session, trace_id=None)
    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/trace")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["trace_id"] is None
    assert data["spans"] == []


def test_run_trace_404_for_unknown_run(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/workflow-runs/WR-missing/trace").status_code == 404


def test_run_trace_maps_spans_when_mlflow_available(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    run_id = _seed_run(client, db_session, trace_id="trace-viewer")

    trace = SimpleNamespace(
        data=SimpleNamespace(
            spans=[
                SimpleNamespace(
                    span_id="s-root",
                    parent_id=None,
                    name="workflow.run",
                    span_type="CHAIN",
                    start_time_ns=0,
                    end_time_ns=4_000_000,
                    status=SimpleNamespace(status_code="OK"),
                    inputs=None,
                    outputs=None,
                    attributes={},
                )
            ]
        )
    )
    fake_mlflow = SimpleNamespace(
        get_trace=lambda trace_id, silent=True: trace,
        get_tracking_uri=lambda: "http://localhost:5000",
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    r = client.get(f"{PREFIX}/workflow-runs/{run_id}/trace")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["trace_id"] == "trace-viewer"
    assert len(data["spans"]) == 1
    assert data["spans"][0]["name"] == "workflow.run"
    assert data["spans"][0]["duration_ms"] == 4.0
    assert data["mlflow_url"] == "http://localhost:5000/#/traces/trace-viewer"


def test_preview_and_compile_emit_metrics(client: TestClient) -> None:
    # ext C1: workflow operations record Prometheus counters.
    _wid, vid = _support_version(client)
    client.post(f"{PREFIX}/workflow-versions/{vid}/compile")
    client.post(
        f"{PREFIX}/workflow-versions/{vid}/preview-run",
        json={"input": "What is your refund policy?"},
    )
    metrics_text = client.get(f"{PREFIX}/metrics").text
    assert "caliber_workflow_compiles_total" in metrics_text
    assert "caliber_workflow_previews_total" in metrics_text


def test_compile_stores_bundle_and_export_uses_it(client: TestClient) -> None:
    _wid, vid = _support_version(client)
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/compile")
    assert r.status_code == 200
    version = client.get(f"{PREFIX}/workflow-versions/{vid}").json()["data"]
    assert version["compiled_artifact_uri"].startswith("caliber-workflow://")
    assert version["compiled_bundle"] is not None
    assert "def run(" in version["compiled_bundle"]["generated_python"]
    # export/python returns the stored bundle byte-for-byte.
    exported = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python").text
    assert exported == version["compiled_bundle"]["generated_python"]
