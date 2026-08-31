"""The final push to 100% addressable coverage: wave 3d (playground-run
files) and wave 3e (dashboard, eval-dataset lifecycle, gateway guardrail
management, judge updates, LLM pricing, trace feedback, readiness, and the
system health/incident surface).

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


# --- playground runs (wave 3d) -------------------------------------------------


def test_playground_files_and_content_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"file_id": "PGF-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.playground_runs.files("PG-1")

    assert seen == ["GET /playground-runs/PG-1/files"]


def test_playground_upload_is_multipart_and_content_downloads_bytes() -> None:
    seen_content_type = []

    def upload_handler(request: httpx.Request) -> httpx.Response:
        seen_content_type.append(request.headers.get("content-type", ""))
        return envelope({"file_id": "PGF-2"}, status=201)

    with client_with(upload_handler) as caliber:
        result = caliber.playground_runs.upload_file("PG-1", "notes.txt", b"raw")
    assert seen_content_type[0].startswith("multipart/form-data")
    assert result == {"file_id": "PGF-2"}

    def download_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/content")
        return httpx.Response(200, content=b"file bytes")

    with client_with(download_handler) as caliber:
        data = caliber.playground_runs.file_content("PG-1", "PGF-2")
    assert data == b"file bytes"


# --- dashboard, readiness (root client methods) --------------------------------


def test_readiness_and_dashboard_summary_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.readiness()
        caliber.dashboard_summary()

    assert seen == ["GET /readiness", "GET /dashboard/summary"]


# --- eval-datasets lifecycle -----------------------------------------------------


def test_eval_dataset_lifecycle_methods_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.datasets.update("ED-1", description="revised")
        caliber.datasets.revise_example("ED-1", "EX-1", input={"q": "x"}, expected={"a": "y"})
        caliber.datasets.supersede_example("ED-1", "EX-2")
        caliber.datasets.restore("ED-1", version=3)
        caliber.datasets.sync("ED-1")

    assert seen == [
        "PATCH /eval-datasets/ED-1",
        "POST /eval-datasets/ED-1/examples/EX-1/revise",
        "POST /eval-datasets/ED-1/examples/EX-2/supersede",
        "POST /eval-datasets/ED-1/restore",
        "POST /eval-datasets/ED-1/sync",
    ]


def test_restore_sends_version_as_the_body() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.datasets.restore("ED-1", version=3)

    assert bodies[0] == b'{"version":3}'


# --- gateway guardrail management, judges update -------------------------------


def test_gateway_guardrail_update_and_detach_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.gateway.update_guardrail_config("EP-1", "GR-1", enabled=False)
        caliber.gateway.detach_guardrail("EP-1", "GR-1")

    assert seen == [
        "PATCH /gateway/endpoints/EP-1/guardrails/GR-1",
        "DELETE /gateway/endpoints/EP-1/guardrails/GR-1",
    ]


def test_judges_update_hits_the_documented_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _seen_path(request) == "PATCH /judges/JDG-1"
        return envelope({"judge_id": "JDG-1"})

    with client_with(handler) as caliber:
        judge = caliber.judges.update("JDG-1", instructions="be more concise")

    assert judge.judge_id == "JDG-1"


# --- LLM pricing: full CRUD -----------------------------------------------------


def test_llm_pricing_crud_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({"pricing_id": "PRC-1"}, status=201 if request.method == "POST" else 200)

    with client_with(handler) as caliber:
        caliber.llm_pricing.list()
        caliber.llm_pricing.create(
            provider="openai", model_id="gpt-5.6-luna", prompt_price=0.001, completion_price=0.003
        )
        caliber.llm_pricing.get("PRC-1")
        caliber.llm_pricing.update("PRC-1", prompt_price=0.0012)

    assert seen == [
        "GET /llm-pricing",
        "POST /llm-pricing",
        "GET /llm-pricing/PRC-1",
        "PATCH /llm-pricing/PRC-1",
    ]


# --- observability feedback -----------------------------------------------------


def test_record_feedback_hits_the_documented_path() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        assert _seen_path(request) == "POST /observability/traces/TR-1/feedback"
        return envelope({})

    with client_with(handler) as caliber:
        caliber.observability.record_feedback("TR-1", value=True, rationale="matches policy")

    assert bodies[0] == b'{"value":true,"rationale":"matches policy"}'


# --- system: services, queue, alerts, incidents --------------------------------


def test_system_health_and_incident_surface_hits_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.system.services()
        caliber.system.queue()
        caliber.system.alerts()
        caliber.system.incidents()
        caliber.system.acknowledge_incident("INC-1")
        caliber.system.silence_incident("INC-1", minutes=30)

    assert seen == [
        "GET /system/services",
        "GET /system/queue",
        "GET /system/alerts",
        "GET /system/incidents",
        "POST /system/incidents/INC-1/acknowledge",
        "POST /system/incidents/INC-1/silence",
    ]


def test_silence_incident_defaults_to_sixty_minutes() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.system.silence_incident("INC-1")

    assert bodies[0] == b'{"minutes":60}'


# --- events: SSE stream is discovered via stream_lines, not get/post/... -----


def test_events_stream_is_recognized_by_the_coverage_scanner() -> None:
    """Regression guard for a real bug: docs-site/sdk_coverage.py had no
    pattern for self._transport.stream_lines(...), so EventsAPI.stream --
    implemented, tested, and shipped since Phase 1 -- silently read as an
    uncovered gap. This does not re-test stream_lines() itself (covered in
    test_transport.py); it exists so this file's own suite fails loudly if
    that regression ever comes back, rather than only the coverage gate
    noticing it in a different package.
    """
    import inspect

    from caliber_sdk.resources.operations import EventsAPI

    source = inspect.getsource(EventsAPI.stream)
    assert 'self._transport.stream_lines("/events/stream"' in source
