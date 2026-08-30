"""Finishes the ``tools`` GA family to 100%: archive, baseline, source,
usage, versions, workspace, saved test cases, sandbox test-invoke, durable
test-run history, and the real queued-calibration pairing.

Every test pins the exact path and method, per the discipline established for
every prior wave: this is precisely the class of bug
``test_sdk_api_coverage.py`` exists to catch.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import Tool, decode

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _seen_path(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}"


# --- calibrate() vs submit_calibration_job(): the pairing this PR fixed ------


def test_calibrate_is_synchronous_and_pass_rate_decodes_correctly() -> None:
    """``calibrate`` hits the synchronous route and has no job_id/status in
    its response -- pass_rate is the one field that genuinely overlaps
    CalibrationJob's shape, and it must decode correctly even though
    job_id/status fall back to their defaults."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/calibrate")
        return envelope({"tool_id": "TL-1", "pass_rate": 0.8, "total": 5, "passed": 4})

    with client_with(handler) as caliber:
        result = caliber.tools.calibrate("TL-1")

    assert result.job_id == ""  # not present in a synchronous response
    assert result.pass_rate == 0.8
    assert result.extra["total"] == 5


def test_submit_calibration_job_hits_the_queued_route_and_decodes_a_real_job_id() -> None:
    """The route ``wait_for_calibration`` actually needs to pair with --
    unlike ``calibrate``, this one's response has a real job_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/calibration-jobs")
        return envelope({"job_id": "CAL-1", "tool_id": "TL-1", "status": "queued"}, status=202)

    with client_with(handler) as caliber:
        job = caliber.tools.submit_calibration_job("TL-1")

    assert job.job_id == "CAL-1"
    assert job.status == "queued"
    assert not job.is_terminal


def test_resolve_calibration_job_sends_action_and_reason() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"resolution": "abandon"})

    with client_with(handler) as caliber:
        caliber.tools.resolve_calibration_job(
            "TL-1", "CAL-1", action="abandon", reason="stuck for an hour"
        )

    assert bodies[0] == b'{"action":"abandon","reason":"stuck for an hour"}'


# --- lifecycle: archive, baseline, versions ----------------------------------


def test_archive_decodes_the_archived_tool() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _seen_path(request) == "POST /tools/TL-1/archive"
        return envelope({"tool_id": "TL-1", "status": "archived"})

    with client_with(handler) as caliber:
        tool = caliber.tools.archive("TL-1")

    assert tool.status == "archived"


def test_set_baseline_sends_the_test_run_id() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"baseline_run_id": "TTR-1"})

    with client_with(handler) as caliber:
        caliber.tools.set_baseline("TL-1", test_run_id="TTR-1")

    assert bodies[0] == b'{"test_run_id":"TTR-1"}'


def test_versions_decodes_a_list_of_tools() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _seen_path(request) == "GET /tools/TL-1/versions"
        return envelope([{"tool_id": "TL-1", "version": "1.0.0"}])

    with client_with(handler) as caliber:
        versions = caliber.tools.versions("TL-1")

    assert [decode(Tool, {"tool_id": v.tool_id}).tool_id for v in versions] == ["TL-1"]


# --- reads: source, usage, workspace -----------------------------------------


def test_source_usage_and_workspace_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.tools.source("TL-1")
        caliber.tools.usage("TL-1")
        caliber.tools.workspace("TL-1")

    assert seen == [
        "GET /tools/TL-1/source",
        "GET /tools/TL-1/usage",
        "GET /tools/TL-1/workspace",
    ]


# --- test cases + sandbox invoke ----------------------------------------------


def test_save_test_cases_sends_the_whole_replacement_set() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"tool_id": "TL-1", "test_cases": []})

    with client_with(handler) as caliber:
        caliber.tools.save_test_cases(
            "TL-1", [{"name": "case-1", "input": {"x": 1}, "assertion": {}}]
        )

    assert bodies[0] == (b'{"test_cases":[{"name":"case-1","input":{"x":1},"assertion":{}}]}')


def test_test_invoke_hits_the_singular_ad_hoc_route() -> None:
    """Distinct from the durable ``/tools/test-runs`` collection -- this is
    the one-off sandbox preview, scoped to a single tool by path."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert _seen_path(request) == "POST /tools/TL-1/test-run"
        return envelope({"tool_id": "TL-1", "output": {"ok": True}, "mocked": False})

    with client_with(handler) as caliber:
        result = caliber.tools.test_invoke("TL-1", input={"query": "refund status"})

    assert result["output"] == {"ok": True}


# --- durable test-run history --------------------------------------------------


def test_tool_test_run_lifecycle_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"test_run_id": "TTR-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.tools.create_test_run(
            tool_id="TL-1", results=[{"case_id": "c1", "verdict": "pass", "score": 1.0}]
        )
        caliber.tools.test_runs(tool_id="TL-1")
        caliber.tools.test_run("TTR-1")

    assert seen == [
        "POST /tools/test-runs",
        "GET /tools/test-runs",
        "GET /tools/test-runs/TTR-1",
    ]
