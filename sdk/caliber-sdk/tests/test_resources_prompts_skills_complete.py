"""Finishes the ``prompts`` and ``skills`` GA families to 100%: template
library, calibration/optimization run submission, durable test-run history,
package import/export, and workspace reads.

Every test pins the exact path and method, per the discipline established for
the governance-verb and agent-CRUD waves: this is precisely the class of bug
``test_sdk_api_coverage.py`` exists to catch.
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


# --- prompts -------------------------------------------------------------------


def test_prompt_reads_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.version("support-triage", 3)
        caliber.prompts.workspace("support-triage")
        caliber.prompts.test_render("support-triage", variables={"topic": "refunds"})
        caliber.prompts.template_library()

    assert seen == [
        "GET /prompts/support-triage/versions/3",
        "GET /prompts/support-triage/workspace",
        "POST /prompts/support-triage/test-render",
        "GET /prompts/template-library",
    ]


def test_prompt_preview_template_sends_base_template_and_extra_params() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.preview_template(base_template_id="support-base", modifier_ids=["concise"])

    assert bodies[0] == b'{"base_template_id":"support-base","modifier_ids":["concise"]}'


def test_prompt_calibration_and_optimization_are_both_modelled_and_share_a_handler() -> None:
    """Two URLs, one server handler -- both still need their own SDK
    coverage, since the coverage gate tracks paths, not handlers."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.calibration_options()
        caliber.prompts.optimization_options()
        caliber.prompts.create_calibration_run(agent_id="AGT-1")
        caliber.prompts.create_optimization_run(agent_id="AGT-1")

    assert seen == [
        "GET /prompts/calibration/options",
        "GET /prompts/optimization/options",
        "POST /prompts/calibration/runs",
        "POST /prompts/optimization/runs",
    ]


def test_prompt_test_run_lifecycle_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"test_run_id": "PTR-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.prompts.create_test_run(
            agent_id="AGT-1", results=[{"case_id": "c1", "verdict": "pass", "score": 1.0}]
        )
        caliber.prompts.test_runs(agent_id="AGT-1")
        caliber.prompts.test_run("PTR-1")

    assert seen == [
        "POST /prompts/test-runs",
        "GET /prompts/test-runs",
        "GET /prompts/test-runs/PTR-1",
    ]


# --- skills ----------------------------------------------------------------------


def test_skill_reads_and_package_preview_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.skills.workspace("SKL-1")
        caliber.skills.package("SKL-1")

    assert seen == ["GET /skills/SKL-1/workspace", "GET /skills/SKL-1/package"]


def test_skill_package_zip_downloads_raw_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/package.zip")
        return httpx.Response(200, content=b"PK\x03\x04fake-zip-bytes")

    with client_with(handler) as caliber:
        data = caliber.skills.package_zip("SKL-1")

    assert data == b"PK\x03\x04fake-zip-bytes"


def test_skill_import_package_sends_files_and_owner() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"skill_id": "SKL-2"}, status=201)

    with client_with(handler) as caliber:
        caliber.skills.import_package(
            [{"path": "SKILL.md", "content": "---\nname: my-skill\n---\nBody"}],
            owner="ops",
            category="support",
        )

    assert bodies[0] == (
        b'{"files":[{"path":"SKILL.md","content":"---\\nname: my-skill\\n---\\nBody"}],'
        b'"owner":"ops","category":"support"}'
    )


def test_skill_import_package_zip_is_multipart_not_json() -> None:
    seen_content_type = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_content_type.append(request.headers.get("content-type", ""))
        return envelope({"skill_id": "SKL-3", "status": "imported"}, status=201)

    with client_with(handler) as caliber:
        result = caliber.skills.import_package_zip(
            "my-skill.zip",
            b"PK\x03\x04zip-bytes",
            conflict_strategy="rename",
            rename_to="my-skill-2",
        )

    assert seen_content_type[0].startswith("multipart/form-data")
    assert result == {"skill_id": "SKL-3", "status": "imported"}


def test_skill_test_run_lifecycle_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"test_run_id": "STR-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.skills.create_test_run(
            skill_id="SKL-1", results=[{"case_id": "c1", "verdict": "pass", "score": 1.0}]
        )
        caliber.skills.test_runs(skill_id="SKL-1")
        caliber.skills.test_run("STR-1")

    assert seen == [
        "POST /skills/test-runs",
        "GET /skills/test-runs",
        "GET /skills/test-runs/STR-1",
    ]
