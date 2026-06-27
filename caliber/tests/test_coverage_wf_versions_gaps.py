"""Tests covering remaining gaps in routes/workflow_versions.py (92% → 100%).

Targets: validate parse-error, publish deprecated→409, preview compile→400,
propose-patch manifest/compile errors→400, list patches/runs missing-workflow
404, export-python fallback compilation, list/get runs, run-by-trace 404.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkflow, CaliberWorkflowVersion
from caliber.ids import new_workflow_id, new_workflow_version_id
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_manifest,
    make_support_manifest,
    register_demo_tools,
)


def _support_workflow(client: TestClient) -> tuple[str, str, str]:
    register_demo_tools(client)
    wid = create_workflow(client, "VersionGap")
    vid, h = create_draft(client, wid, make_support_manifest(wid))
    return wid, vid, h


# ------------------------------------------------------------------
# validate_version — parse_manifest error (lines 220-221)
# ------------------------------------------------------------------


def test_validate_version_parse_error_returns_parse_error(
    client: TestClient, db_session: Session
) -> None:
    """If the stored manifest is malformed enough that parse_manifest raises,
    the endpoint returns a 200 with valid=False and a parse_error code."""
    wf = CaliberWorkflow(
        workflow_id=new_workflow_id(), name="bad-manifest", owner="@test", status="active"
    )
    db_session.add(wf)
    db_session.flush()
    # Store a manifest dict that will fail parse_manifest (missing required fields)
    version = CaliberWorkflowVersion(
        version_id=new_workflow_version_id(),
        workflow_id=wf.workflow_id,
        version_number=1,
        manifest={"bad": True},  # Missing schema_version, nodes, edges, etc.
        manifest_hash="deadbeef",
        status="draft",
    )
    db_session.add(version)
    db_session.commit()
    r = client.post(f"{PREFIX}/workflow-versions/{version.version_id}/validate")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["valid"] is False
    assert any(e["code"] == "parse_error" for e in data["errors"])


# ------------------------------------------------------------------
# publish_version_route — PublishError "deprecated" → 409 (lines 269-272)
# ------------------------------------------------------------------


def test_publish_deprecated_version_returns_409(client: TestClient, db_session: Session) -> None:
    """Publishing a deprecated version raises PublishError with 'deprecated' in the
    message, which the route handler maps to 409."""
    wf = CaliberWorkflow(
        workflow_id=new_workflow_id(), name="deprecated-pub", owner="@test", status="active"
    )
    db_session.add(wf)
    db_session.flush()
    version = CaliberWorkflowVersion(
        version_id=new_workflow_version_id(),
        workflow_id=wf.workflow_id,
        version_number=1,
        manifest=make_manifest(wf.workflow_id),
        manifest_hash="abcd1234",
        status="deprecated",
    )
    db_session.add(version)
    db_session.commit()
    r = client.post(f"{PREFIX}/workflow-versions/{version.version_id}/publish")
    assert r.status_code == 409
    assert "deprecated" in r.json()["detail"]


# ------------------------------------------------------------------
# preview_run_route — CompileError → 400 (lines 300-301)
# ------------------------------------------------------------------


def test_preview_run_compile_error_returns_400(client: TestClient, db_session: Session) -> None:
    """Preview on a version with a non-compilable manifest returns 400."""
    wf = CaliberWorkflow(
        workflow_id=new_workflow_id(), name="preview-bad", owner="@test", status="active"
    )
    db_session.add(wf)
    db_session.flush()
    # Manifest that parses but won't compile (bad node references)
    bad = make_manifest(wf.workflow_id)
    bad["edges"][0]["from"] = "nonexistent"
    version = CaliberWorkflowVersion(
        version_id=new_workflow_version_id(),
        workflow_id=wf.workflow_id,
        version_number=1,
        manifest=bad,
        manifest_hash="preview123",
        status="draft",
    )
    db_session.add(version)
    db_session.commit()
    r = client.post(
        f"{PREFIX}/workflow-versions/{version.version_id}/preview-run",
        json={"input": "hello"},
    )
    assert r.status_code == 400
    assert "cannot preview" in r.json()["detail"]


# ------------------------------------------------------------------
# propose_patch_route — base manifest parse error (lines 324-325)
# ------------------------------------------------------------------


def test_propose_patch_bad_base_manifest_returns_400(
    client: TestClient, db_session: Session
) -> None:
    """If the base version's manifest doesn't parse, we get 400."""
    wf = CaliberWorkflow(
        workflow_id=new_workflow_id(), name="patch-bad", owner="@test", status="active"
    )
    db_session.add(wf)
    db_session.flush()
    version = CaliberWorkflowVersion(
        version_id=new_workflow_version_id(),
        workflow_id=wf.workflow_id,
        version_number=1,
        manifest={"broken": True},
        manifest_hash="patchbad",
        status="draft",
    )
    db_session.add(version)
    db_session.commit()
    r = client.post(
        f"{PREFIX}/workflow-versions/{version.version_id}/propose-patch",
        json={"evidence": {"failure_type": "quality_regression"}},
    )
    assert r.status_code == 400
    assert "invalid base manifest" in r.json()["detail"]


# ------------------------------------------------------------------
# list_patches_route — missing workflow → 404 (line 395)
# ------------------------------------------------------------------


def test_list_patches_missing_workflow_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows/WF-NOPE/patches")
    assert r.status_code == 404


# ------------------------------------------------------------------
# list_runs_route — missing workflow → 404 (line 416)
# ------------------------------------------------------------------


def test_list_runs_missing_workflow_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows/WF-NOPE/runs")
    assert r.status_code == 404


# ------------------------------------------------------------------
# list_runs_route — empty result (line 416+)
# ------------------------------------------------------------------


def test_list_runs_empty(client: TestClient) -> None:
    register_demo_tools(client)
    wid = create_workflow(client, "RunsEmpty")
    r = client.get(f"{PREFIX}/workflows/{wid}/runs")
    assert r.status_code == 200
    assert r.json()["data"] == []


# ------------------------------------------------------------------
# run_by_trace_route — missing trace → 404
# ------------------------------------------------------------------


def test_run_by_trace_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflow-runs/by-trace/t-does-not-exist")
    assert r.status_code == 404


# ------------------------------------------------------------------
# export_python_route — fallback compilation (lines 476-486)
# ------------------------------------------------------------------


def test_export_python_fallback_no_compiled_bundle(client: TestClient) -> None:
    """When compiled_bundle is absent, export-python falls back to
    compile_workflow on the fly."""
    _wid, vid, _ = _support_workflow(client)
    # The draft is not compiled yet, so compiled_bundle is None.
    # export/python should still succeed via the fallback.
    r = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python")
    assert r.status_code == 200
    assert "def run(" in r.text


def test_export_python_published_uses_bundle(client: TestClient) -> None:
    """After publish, the stored compiled_bundle is used directly."""
    _wid, vid, _ = _support_workflow(client)
    client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    r = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python")
    assert r.status_code == 200
    assert "def run(" in r.text
