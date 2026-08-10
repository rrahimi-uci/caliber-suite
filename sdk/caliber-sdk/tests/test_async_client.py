"""The async client, and the parity that keeps it from drifting from the sync one.

Tests drive coroutines with ``asyncio.run`` inside ordinary test functions rather
than adding ``pytest-asyncio``. One less dev dependency, and the boundary between
"the loop" and "the assertion" stays visible in each test.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient
from caliber_sdk.aio import AsyncCaliberClient
from caliber_sdk.errors import CaliberNotFoundError, CaliberTransportError

BASE = "https://caliber.test"


def client_with(handler: Any) -> AsyncCaliberClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncCaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the basics -----------------------------------------------------------


def test_the_async_client_reads_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me")
        return envelope({"user_id": "@alice", "scopes": ["caliber.admin"]})

    async def main() -> Any:
        async with client_with(handler) as caliber:
            return await caliber.me.get()

    identity = run(main())
    assert identity.user_id == "@alice"
    assert not identity.is_anonymous


def test_the_async_client_sends_the_same_credential_header_as_the_sync_one() -> None:
    """The precedence rule lives in one place, so this proves it is shared.

    A token beats a trusted header. Two clients that authenticated differently
    would be the worst kind of divergence: invisible until a permission failure.
    """
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen[request.headers.get("user-agent", "")[:0] or "auth"] = request.headers.get(
            "authorization", ""
        )
        return envelope({"user_id": "@alice"})

    sync_seen: dict[str, str] = {}

    def capture_sync(request: httpx.Request) -> httpx.Response:
        sync_seen["auth"] = request.headers.get("authorization", "")
        return envelope({"user_id": "@alice"})

    async def main() -> None:
        async with client_with(capture) as caliber:
            await caliber.me.get()

    run(main())
    with CaliberClient(
        BASE,
        token="calpat_test",
        user="@someone",
        http_client=httpx.Client(transport=httpx.MockTransport(capture_sync)),
    ) as sync:
        sync.me.get()

    assert seen["auth"].startswith("Bearer ")
    assert seen["auth"] == sync_seen["auth"]


def test_an_error_response_raises_the_same_typed_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such run"})

    async def main() -> None:
        async with client_with(handler) as caliber:
            await caliber.workflows.get("RUN-nope")

    with pytest.raises(CaliberNotFoundError) as caught:
        run(main())
    assert caught.value.status_code == 404
    assert caught.value.detail == "no such run"


def test_a_transport_failure_becomes_a_caliber_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async def main() -> None:
        async with client_with(handler) as caliber:
            await caliber.me.get()

    with pytest.raises(CaliberTransportError):
        run(main())


def test_the_envelope_is_unwrapped_only_when_the_body_is_exactly_one_data_key() -> None:
    """The OpenAPI document is served unenveloped; unwrapping it yields None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openapi": "3.0.3", "paths": {}})

    async def main() -> Any:
        async with client_with(handler) as caliber:
            return await caliber.raw.get("/openapi.json")

    assert run(main())["openapi"] == "3.0.3"


# --- concurrency ----------------------------------------------------------


def test_many_runs_can_be_submitted_concurrently() -> None:
    """The request-side reason to be async: forty coroutines, not forty threads."""
    submitted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = jsonlib.loads(request.content)
        submitted.append(body["input"])
        return envelope({"workflow_run_id": f"RUN-{body['input']}", "status": "queued"})

    async def main() -> Any:
        async with client_with(handler) as caliber:
            return await asyncio.gather(
                *(
                    caliber.workflows.submit(workflow_version_id="WFV-1", input=str(index))
                    for index in range(10)
                )
            )

    runs = run(main())
    assert sorted(submitted) == sorted(str(index) for index in range(10))
    assert len({item.workflow_run_id for item in runs}) == 10


# --- waiting --------------------------------------------------------------


def test_waiting_returns_when_the_run_reaches_a_terminal_state() -> None:
    states = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "RUN-1", "status": next(states)})

    async def main() -> Any:
        async with client_with(handler) as caliber:
            return await caliber.workflows.wait(
                "RUN-1", interval=0.001, max_interval=0.001, timeout=5
            )

    assert run(main()).status == "succeeded"


def test_waiting_on_a_job_returns_when_it_stops_for_a_person() -> None:
    """``candidate_ready`` is a resting state, not a transient one.

    Polling past it would spend the whole timeout waiting for something that
    cannot happen without a human.
    """
    states = iter(["queued", "running", "candidate_ready"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"job_id": "RFN-1", "status": next(states)})

    async def main() -> Any:
        async with client_with(handler) as caliber:
            return await caliber.jobs.wait("RFN-1", interval=0.001, max_interval=0.001, timeout=5)

    job = run(main())
    assert job.awaits_human
    assert not job.is_terminal


def test_a_failed_run_raises_by_default_and_can_be_inspected_instead() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "RUN-1", "status": "failed"})

    async def raising() -> None:
        async with client_with(handler) as caliber:
            await caliber.workflows.wait("RUN-1", timeout=5)

    async def inspecting() -> Any:
        async with client_with(handler) as caliber:
            return await caliber.workflows.wait("RUN-1", timeout=5, raise_on_failure=False)

    from caliber_sdk.resources.workflows import WorkflowRunFailed

    with pytest.raises(WorkflowRunFailed):
        run(raising())
    assert run(inspecting()).status == "failed"


def test_a_wait_that_times_out_reports_the_last_state_it_saw() -> None:
    from caliber_sdk.aio.waiters import WaitTimeout

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "RUN-1", "status": "running"})

    async def main() -> None:
        async with client_with(handler) as caliber:
            await caliber.workflows.wait("RUN-1", interval=0.001, max_interval=0.001, timeout=0.01)

    with pytest.raises(WaitTimeout) as caught:
        run(main())
    assert caught.value.last.status == "running"


# --- streaming: the case async exists for ---------------------------------


def test_the_event_stream_yields_lines_as_they_arrive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, content=b"event: ping\ndata: {}\n\nevent: pong\ndata: {}\n\n")

    async def main() -> list[str]:
        async with client_with(handler) as caliber:
            return [line async for line in caliber.events.stream()]

    lines = run(main())
    assert "event: ping" in lines
    assert "event: pong" in lines


def test_a_stream_that_fails_raises_before_yielding_anything() -> None:
    """The error body must be read and reported, not delivered as event lines."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "scope caliber.operator required"})

    async def main() -> list[str]:
        async with client_with(handler) as caliber:
            return [line async for line in caliber.events.stream()]

    from caliber_sdk.errors import CaliberPermissionError

    with pytest.raises(CaliberPermissionError) as caught:
        run(main())
    assert "caliber.operator" in str(caught.value)


# --- retries --------------------------------------------------------------


def test_a_retryable_status_is_retried_without_blocking_the_loop() -> None:
    """Awaited rather than slept: a blocking sleep would stall every other
    coroutine sharing the loop, which is the specific thing async is for."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, json={"detail": "unavailable"})
        return envelope({"user_id": "@alice"})

    async def main() -> Any:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with AsyncCaliberClient(BASE, token="t", http_client=http, max_retries=3) as caliber:
            return await caliber.me.get()

    assert run(main()).user_id == "@alice"
    assert attempts["count"] == 3


def test_a_post_is_never_retried() -> None:
    """The SDK cannot know whether the failure happened before or after the
    server performed the effect."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503, json={"detail": "unavailable"})

    async def main() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with AsyncCaliberClient(BASE, token="t", http_client=http, max_retries=3) as caliber:
            await caliber.workflows.submit(workflow_version_id="WFV-1")

    from caliber_sdk.errors import CaliberServerError

    with pytest.raises(CaliberServerError):
        run(main())
    assert attempts["count"] == 1


# --- lifetime -------------------------------------------------------------


def test_a_caller_supplied_client_is_not_closed_by_the_sdk() -> None:
    """Its lifetime belongs to whoever created it; closing it here would break
    the next thing that used it."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: envelope({})))

    async def main() -> bool:
        async with AsyncCaliberClient(BASE, token="t", http_client=http):
            pass
        return http.is_closed

    assert run(main()) is False
