"""Finishes ``workflow-runs`` to 100%: the execution/debugging surface --
trace lookup, event-driven resume, checkpoints, human-approval decisions,
run files/artifacts, and observability reads (events, trace, lineage,
manifest).

Every test pins the exact path and method, per the discipline established for
every prior wave.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _seen_path(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}"


# --- lookup, resume, retry -----------------------------------------------------


def test_by_trace_decodes_a_workflow_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _seen_path(request) == "GET /workflow-runs/by-trace/TR-1"
        return envelope({"workflow_run_id": "WFR-1", "status": "success"})

    with client_with(handler) as caliber:
        run = caliber.workflows.runs.by_trace("TR-1")

    assert run.workflow_run_id == "WFR-1"


def test_resume_by_event_resume_and_retry_hit_distinct_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.runs.resume_by_event(event_name="order.paid")
        caliber.workflows.runs.resume("WFR-1")
        caliber.workflows.runs.retry("WFR-1")

    assert seen == [
        "POST /workflow-runs/resume-by-event",
        "POST /workflow-runs/WFR-1/resume",
        "POST /workflow-runs/WFR-1/retry",
    ]


# --- observability reads: events, trace, lineage, manifest --------------------


def test_events_trace_lineage_and_manifest_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.runs.events("WFR-1")
        caliber.workflows.runs.trace("WFR-1")
        caliber.workflows.runs.lineage("WFR-1")
        caliber.workflows.runs.manifest("WFR-1")

    assert seen == [
        "GET /workflow-runs/WFR-1/events",
        "GET /workflow-runs/WFR-1/trace",
        "GET /workflow-runs/WFR-1/lineage",
        "GET /workflow-runs/WFR-1/manifest",
    ]


def test_events_and_checkpoints_pass_after_and_limit_as_params() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.runs.events("WFR-1", after="42", limit=50)
        caliber.workflows.runs.checkpoints("WFR-1", after="7", limit=10)

    assert captured[0] == {"after": "42", "limit": "50"}
    assert captured[1] == {"after": "7", "limit": "10"}


# --- human approval: list, approve, reject -------------------------------------


def test_approvals_approve_and_reject_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.runs.approvals("WFR-1")
        caliber.workflows.runs.approve("WFR-1", node_id="approve-1", reason="looks fine")
        caliber.workflows.runs.reject("WFR-1", node_id="approve-1", reason="needs changes")

    assert seen == [
        "GET /workflow-runs/WFR-1/approvals",
        "POST /workflow-runs/WFR-1/approval/approve",
        "POST /workflow-runs/WFR-1/approval/reject",
    ]


# --- run files: list, upload, detail, download, register as artifact --------


def test_files_file_and_register_artifact_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"file_id": "WRF-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.workflows.runs.files("WFR-1")
        caliber.workflows.runs.file("WFR-1", "WRF-1")
        caliber.workflows.runs.register_artifact(
            "WFR-1", file_id="WRF-1", display_name="report.csv"
        )

    assert seen == [
        "GET /workflow-runs/WFR-1/files",
        "GET /workflow-runs/WFR-1/files/WRF-1",
        "POST /workflow-runs/WFR-1/artifacts",
    ]


def test_upload_file_is_multipart_not_json() -> None:
    seen_content_type = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_content_type.append(request.headers.get("content-type", ""))
        assert request.url.path.endswith("/files")
        return envelope({"file_id": "WRF-2"}, status=201)

    with client_with(handler) as caliber:
        result = caliber.workflows.runs.upload_file("WFR-1", "output.json", b'{"ok": true}')

    assert seen_content_type[0].startswith("multipart/form-data")
    assert result == {"file_id": "WRF-2"}


def test_file_content_downloads_raw_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/content")
        return httpx.Response(200, content=b"raw file bytes")

    with client_with(handler) as caliber:
        data = caliber.workflows.runs.file_content("WFR-1", "WRF-1")

    assert data == b"raw file bytes"


# --- the admission manifest (B02.5, #695) -------------------------------------


def test_admission_manifest_hits_its_own_path_and_decodes_the_typed_record() -> None:
    from caliber_sdk.models import AdmissionManifest

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.url.path.endswith("WFR-legacy/admission-manifest"):
            return envelope({"workflow_run_id": "WFR-legacy", "legacy": True})
        return envelope(
            {
                "workflow_run_id": "WFR-1",
                "legacy": False,
                "schema_version": "v1",
                "manifest_id": "mf-1",
                "run_manifest": {"pins": {"workflow_version": "WFV-1"}},
                "manifest_digest": "a" * 64,
                "future_field": 1,
            }
        )

    with client_with(handler) as caliber:
        admitted = caliber.workflows.runs.admission_manifest("WFR-1")
        legacy = caliber.workflows.runs.admission_manifest("WFR-legacy")
        caliber.workflows.runs.manifest("WFR-1")

    assert seen == [
        "GET /workflow-runs/WFR-1/admission-manifest",
        "GET /workflow-runs/WFR-legacy/admission-manifest",
        "GET /workflow-runs/WFR-1/manifest",
    ], "the workflow manifest endpoint is a different one"
    assert isinstance(admitted, AdmissionManifest)
    assert admitted.legacy is False and admitted.manifest_digest == "a" * 64
    assert admitted.run_manifest == {"pins": {"workflow_version": "WFV-1"}}
    assert admitted.extra == {"future_field": 1}
    assert legacy.legacy is True and legacy.run_manifest is None and legacy.manifest_digest is None


def test_an_admission_manifest_that_does_not_say_whether_it_is_legacy_is_not_read_as_admitted() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "WFR-1"})

    with client_with(handler) as caliber:
        unknown = caliber.workflows.runs.admission_manifest("WFR-1")

    assert unknown.legacy is None and unknown.run_manifest is None
