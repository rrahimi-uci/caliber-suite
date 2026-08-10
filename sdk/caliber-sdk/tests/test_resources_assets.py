"""Prompt, skill, and tool resource modules."""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import CalibrationJob, decode

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


# --- prompts --------------------------------------------------------------


def test_prompts_decode_registry_coordinates() -> None:
    """A prompt is an MLflow registry object, not a CALIBER row."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            [{"agent_id": "support", "prompt_name": "support", "version": 3, "alias": "prod"}]
        )

    with client_with(handler) as caliber:
        prompts = caliber.prompts.list()

    assert prompts[0].version == 3
    assert prompts[0].alias == "prod"


def test_registering_a_version_does_not_touch_the_alias() -> None:
    """Authoring and deployment are separate calls, and must stay separate.

    The refinement loop depends on being able to register a candidate without
    it becoming live, so these are two endpoints rather than one flag.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        return envelope({"version": 4})

    with client_with(handler) as caliber:
        caliber.prompts.register_version("support", "new template", commit_message="tighten")
        caliber.prompts.promote("support", 4)

    assert seen == ["POST /prompts/support/versions", "POST /prompts/support/promote"]


def test_promote_sends_the_alias_it_was_given() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(jsonlib.loads(request.content))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.promote("support", 7, alias="staging")

    assert sent == {"version": 7, "alias": "staging"}


# --- skills ---------------------------------------------------------------


def test_skill_render_reports_unresolved_variables() -> None:
    """The field that tells an author their template is incomplete."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "skill_id": "SK-1",
                "skill_name": "triage",
                "rendered_content": "hello {{missing}}",
                "original_content": "hello {{missing}}",
                "detected_variables": ["missing"],
                "unresolved_variables": ["missing"],
                "variables_applied": {},
                "word_count": 2,
            }
        )

    with client_with(handler) as caliber:
        rendered = caliber.skills.render("SK-1", variables={})

    assert rendered.unresolved_variables == ["missing"]
    assert rendered.word_count == 2


def test_skill_selection_reports_score_and_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "skill_id": "SK-1",
                "skill_name": "triage",
                "is_selected": True,
                "selection_score": 0.8,
                "selection_reason": "matched trigger",
            }
        )

    with client_with(handler) as caliber:
        selection = caliber.skills.test_selection("SK-1", "refund please")

    assert selection.is_selected
    assert selection.selection_score == 0.8


def test_skill_list_filters_are_sent_as_params() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return envelope([])

    with client_with(handler) as caliber:
        caliber.skills.list(status="active", tag="support")

    assert seen == {"status": "active", "tag": "support"}


# --- tools ----------------------------------------------------------------


def test_calibration_terminal_states() -> None:
    assert decode(CalibrationJob, {"status": "succeeded"}).is_terminal
    assert decode(CalibrationJob, {"status": "failed"}).is_terminal
    assert not decode(CalibrationJob, {"status": "queued"}).is_terminal


def test_waiting_for_calibration_returns_the_failed_job_rather_than_raising() -> None:
    """A failed calibration is a result to inspect, not a call that errored.

    The generic waiter raises on failure states by default; this one
    deliberately does not, because the caller wants the scores either way.
    """
    states = iter(["queued", "running", "failed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"job_id": "CAL-1", "status": next(states), "result": {"pass_rate": 0.4}})

    with client_with(handler) as caliber:
        job = caliber.tools.wait_for_calibration(
            "TOOL-1", "CAL-1", interval=0.001, max_interval=0.001, timeout=5
        )

    assert job.status == "failed"
    assert job.result == {"pass_rate": 0.4}


def test_calibration_jobs_unwraps_the_jobs_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"jobs": [{"job_id": "CAL-1", "status": "queued"}], "total": 1})

    with client_with(handler) as caliber:
        jobs = caliber.tools.calibration_jobs("TOOL-1")

    assert [j.job_id for j in jobs] == ["CAL-1"]


def test_tool_schemas_stay_open_mappings() -> None:
    """JSON Schema is the caller's data; the SDK stores it, not defines it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "tool_id": "T-1",
                "name": "lookup",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "string"},
            }
        )

    with client_with(handler) as caliber:
        tool = caliber.tools.get("T-1")

    assert tool.input_schema == {"type": "object"}
    assert tool.output_schema == {"type": "string"}
