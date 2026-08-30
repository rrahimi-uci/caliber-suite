"""Finishes ``workflow-versions`` to 100% (draft editing, restore, diff,
export, preview/real runs, patch proposal, copilot/plan-build) plus the
small standalone workflow surfaces: patches, components/templates/
cron-preview catalogs, staging uploads, and benchmark-report CRUD.

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


# --- version lifecycle: update, restore, diff --------------------------------


def test_update_sends_manifest_and_hash_for_optimistic_concurrency() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"version_id": "WFV-1", "status": "draft"})

    with client_with(handler) as caliber:
        caliber.workflows.versions.update("WFV-1", manifest={"nodes": []}, manifest_hash="abc123")

    assert bodies[0] == b'{"manifest":{"nodes":[]},"manifest_hash":"abc123"}'


def test_restore_and_diff_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.versions.restore("WFV-1")
        caliber.workflows.versions.diff("WFV-1", "WFV-2")

    assert seen == [
        "POST /workflow-versions/WFV-1/restore",
        "GET /workflow-versions/WFV-1/diff/WFV-2",
    ]


# --- export: plain text, not JSON --------------------------------------------


def test_export_manifest_returns_decoded_yaml_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/export/manifest")
        return httpx.Response(
            200, content=b"nodes: []\n", headers={"content-type": "application/x-yaml"}
        )

    with client_with(handler) as caliber:
        text = caliber.workflows.versions.export_manifest("WFV-1")

    assert text == "nodes: []\n"


def test_export_python_returns_decoded_source_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/export/python")
        return httpx.Response(
            200, content=b"def run():\n    pass\n", headers={"content-type": "text/x-python"}
        )

    with client_with(handler) as caliber:
        text = caliber.workflows.versions.export_python("WFV-1")

    assert text == "def run():\n    pass\n"


# --- preview / real run -------------------------------------------------------


def test_preview_run_and_run_hit_distinct_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"status": "success"})

    with client_with(handler) as caliber:
        caliber.workflows.versions.preview_run("WFV-1", input={"q": "hi"})
        caliber.workflows.versions.run("WFV-1", input={"q": "hi"})

    assert seen == [
        "POST /workflow-versions/WFV-1/preview-run",
        "POST /workflow-versions/WFV-1/run",
    ]


# --- copilot: propose_patch, copilot_edit, plan_build ------------------------


def test_propose_patch_sends_evidence_and_optional_job_id() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"patch_id": "PATCH-1"})

    with client_with(handler) as caliber:
        caliber.workflows.versions.propose_patch(
            "WFV-1", evidence={"error": "timeout"}, job_id="JOB-1"
        )

    assert bodies[0] == b'{"evidence":{"error":"timeout"},"job_id":"JOB-1"}'


def test_copilot_edit_and_plan_build_hit_distinct_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.versions.copilot_edit("WFV-1", instruction="add a guardrail node")
        caliber.workflows.versions.plan_build("WFV-1", goal="triage refund requests")

    assert seen == [
        "POST /workflow-versions/WFV-1/copilot-edit",
        "POST /workflow-versions/WFV-1/plan-build",
    ]


# --- patches, static catalogs, cron preview -----------------------------------


def test_patches_components_templates_and_cron_preview_hit_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.patches("WF-1")
        caliber.workflows.components()
        caliber.workflows.templates()
        caliber.workflows.cron_preview(expr="0 9 * * MON")

    assert seen == [
        "GET /workflows/WF-1/patches",
        "GET /workflow-components",
        "GET /workflow-templates",
        "GET /workflow-cron-preview",
    ]


def test_cron_preview_sends_expr_tz_and_count_as_query_params() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.workflows.cron_preview(expr="0 9 * * MON", tz="America/New_York", count=3)

    assert captured[0] == {"expr": "0 9 * * MON", "tz": "America/New_York", "count": "3"}


# --- staging upload (multipart) ----------------------------------------------


def test_upload_staging_file_is_multipart_not_json() -> None:
    seen_content_type = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_content_type.append(request.headers.get("content-type", ""))
        assert request.url.path.endswith("/workflow-files")
        return envelope({"file_id": "WFF-1"}, status=201)

    with client_with(handler) as caliber:
        result = caliber.workflows.upload_staging_file("input.json", b'{"x": 1}', kind="input")

    assert seen_content_type[0].startswith("multipart/form-data")
    assert result == {"file_id": "WFF-1"}


# --- benchmark reports: full CRUD --------------------------------------------


def test_benchmark_reports_crud_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"report_id": "WBR-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.workflows.benchmark_reports.list()
        caliber.workflows.benchmark_reports.create(name="Q3 bakeoff", worksheet={"rows": []})
        caliber.workflows.benchmark_reports.update("WBR-1", status="published")
        caliber.workflows.benchmark_reports.delete("WBR-1")

    assert seen == [
        "GET /workflow-benchmark-reports",
        "POST /workflow-benchmark-reports",
        "PATCH /workflow-benchmark-reports/WBR-1",
        "DELETE /workflow-benchmark-reports/WBR-1",
    ]


def test_benchmark_reports_list_defaults_to_status_all() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return envelope([])

    with client_with(handler) as caliber:
        caliber.workflows.benchmark_reports.list()

    assert captured[0] == {"status": "all"}
