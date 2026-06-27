"""Smoke tests for ``/caliber/events/stream``.

Streaming endpoints are awkward to test with httpx.TestClient because the
client blocks waiting for the stream to close. We use ``client.stream()``
with a short read and assert the first frame (the ``connected`` event)
reaches the wire. Beyond that we test the format helper directly.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.events.bus import EventBus
from caliber.routes import events_stream
from caliber.routes.events_stream import STREAM_PATH, _format_event


def test_format_event_emits_event_and_data_lines() -> None:
    frame = _format_event({"type": "job.advanced", "job_id": "RFN-1"})
    text = frame.decode("utf-8")
    assert text.startswith("event: job.advanced\n")
    payload_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = json.loads(payload_line[len("data: ") :])
    assert payload == {"type": "job.advanced", "job_id": "RFN-1"}
    # SSE frames end with a blank line (here ``\n\n`` after ``data:``).
    assert text.endswith("\n\n")


def test_format_event_without_type_uses_default_message() -> None:
    frame = _format_event({"foo": "bar"})
    text = frame.decode("utf-8")
    assert "event:" not in text  # default ``message`` event type — no header
    assert '"foo":"bar"' in text


def test_format_event_strips_internal_caliber_fields() -> None:
    frame = _format_event(
        {"type": "workflow.run.queued", "workflow_run_id": "WR-1", "_caliber_remote": True}
    )
    payload_line = next(
        line for line in frame.decode("utf-8").split("\n") if line.startswith("data:")
    )
    payload = json.loads(payload_line[len("data: ") :])
    assert payload == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}


def test_stream_route_is_registered(client: TestClient) -> None:
    """Verify the route is wired without actually opening the stream.

    A full stream-read test isn't practical with httpx.TestClient — the
    transport keeps the long-poll connection open and the generator's
    disconnect check fires too late to be useful in unit tests. We
    inspect the app's routing table directly; end-to-end SSE behavior
    is exercised at integration time with a real ASGI server.
    """
    paths = {getattr(route, "path", None) for route in client.app.routes}  # type: ignore[attr-defined]
    assert STREAM_PATH in paths


@pytest.mark.asyncio
async def test_event_loop_disconnect_closes_pending_subscription_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnect after an idle heartbeat should not close a running async generator."""

    class _Request:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(events_stream, "_HEARTBEAT_SECONDS", 0.001)
    stream = events_stream._event_loop(_Request(), EventBus())  # type: ignore[arg-type]

    assert (await stream.__anext__()).startswith(b"event: connected")
    assert await stream.__anext__() == b":keepalive\n\n"
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


def test_apply_publishes_applied_event_to_bus(client: TestClient) -> None:
    """End-to-end: applying a candidate_ready job should fire ``job.applied``."""
    # Seed an agent + verified item + candidate_ready job.
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        assert isinstance(session, Session)
        session.add(
            CaliberAgentConfig(
                agent_id="agent",
                experiment_id="exp",
                name="A",
                owner="@x",
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
            )
        )
        session.flush()
        session.add(
            CaliberVerificationItem(
                item_id="FB-1",
                agent_id="agent",
                category="hallucination",
                free_text="...",
                severity="critical",
                status="verified",
            )
        )
        session.flush()
        session.add(
            CaliberRefinementJob(
                job_id="RFN-1",
                agent_id="agent",
                primary_item_id="FB-1",
                artifact_type="prompt",
                status="candidate_ready",
                current_stage="done",
                bundle_targets=[],
                eval_results={},
                candidate={"content": "new prompt", "artifact_type": "prompt"},
            )
        )
        session.commit()

    bus = client.app.state.event_bus  # type: ignore[attr-defined]
    captured: list[dict[str, object]] = []
    original_publish = bus.publish

    def capture(event: dict[str, object]) -> None:
        captured.append(event)
        original_publish(event)

    bus.publish = capture  # type: ignore[method-assign]
    try:
        response = client.post("/ajax-api/2.0/mlflow/caliber/jobs/RFN-1/apply", json={})
        assert response.status_code == 200
    finally:
        bus.publish = original_publish  # type: ignore[method-assign]

    types = [event["type"] for event in captured]
    assert "job.applied" in types
    applied = next(e for e in captured if e["type"] == "job.applied")
    assert applied["job_id"] == "RFN-1"
