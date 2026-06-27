"""``/caliber/events/stream`` — Server-Sent Events endpoint.

Streams ``data: {...}\\n\\n``-framed events from ``app.state.event_bus``.
That bus may be purely in-process or backed by a cross-replica transport
such as the database or NATS. The SPA opens an ``EventSource``
to this URL on page load and uses each event to update its in-memory
state (job progress bars, queue badges, approval-list highlights)
without polling.

Frame format follows the SSE spec (https://html.spec.whatwg.org/#server-sent-events):

    event: <type>
    data: <json>
    \\n

Heartbeats fire every ``_HEARTBEAT_SECONDS`` as comment lines (``:keepalive``)
so reverse proxies / load balancers don't close idle long-poll connections.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from caliber.auth import require_user
from caliber.events.bus import EventBus

logger = logging.getLogger("caliber.routes.events_stream")

STREAM_PATH = "/ajax-api/2.0/mlflow/caliber/events/stream"

# Reverse proxies (nginx, ELBs) typically idle out connections at 60s.
# Sending a comment-line heartbeat well under that keeps the channel open
# without spamming the wire.
_HEARTBEAT_SECONDS = 15.0


def _get_event_bus(request: Request) -> EventBus:
    bus: EventBus = request.app.state.event_bus
    return bus


async def _event_loop(request: Request, bus: EventBus) -> AsyncIterator[bytes]:
    """Yield SSE frames until the client disconnects."""
    # First frame is the "connected" acknowledgement. Clients use this to
    # confirm the EventSource is open and reset reconnect backoff.
    yield _format_event({"type": "connected"})

    # The bus iterator is unbounded. We race it against a heartbeat timer
    # so an idle period still flushes bytes onto the wire. The subscription
    # is annotated as :class:`AsyncGenerator` because we need the
    # ``aclose()`` method — :class:`AsyncIterator` doesn't expose it.
    subscription: AsyncGenerator[dict[str, Any], None] = bus.subscribe()  # type: ignore[assignment]
    next_event_task: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if await request.is_disconnected():
                return
            if next_event_task is None:
                next_event_task = asyncio.create_task(_anext(subscription))
            try:
                event = await asyncio.wait_for(
                    asyncio.shield(next_event_task), timeout=_HEARTBEAT_SECONDS
                )
            except TimeoutError:
                yield b":keepalive\n\n"
                continue
            next_event_task = None
            yield _format_event(event)
    finally:
        if next_event_task is not None:
            next_event_task.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await next_event_task
        await subscription.aclose()


async def _anext(it: AsyncIterator[dict[str, Any]]) -> dict[str, Any]:
    return await it.__anext__()


def _format_event(event: dict[str, Any]) -> bytes:
    """Render a payload as an SSE ``event:`` + ``data:`` frame.

    Events without a ``type`` key use SSE's default ``message`` type so
    listeners that only subscribe to specific event names still receive
    something (vs. silently dropping unknown frames).
    """
    event = {key: value for key, value in event.items() if not key.startswith("_caliber_")}
    event_type = event.get("type")
    payload = json.dumps(event, separators=(",", ":"), default=str)
    lines: list[str] = []
    if event_type:
        lines.append(f"event: {event_type}")
    lines.append(f"data: {payload}")
    lines.append("")  # blank line terminates the frame
    lines.append("")
    return "\n".join(lines).encode("utf-8")


async def stream_events(request: Request) -> StreamingResponse:
    """SSE endpoint.

    The ``X-Accel-Buffering: no`` header instructs nginx not to buffer
    the response — without it the proxy holds chunks until they fill
    a buffer, which destroys the live-update experience.
    """
    require_user(request)
    bus = _get_event_bus(request)
    return StreamingResponse(
        _event_loop(request, bus),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def register(app: Starlette) -> None:
    """Add the SSE route to the given Starlette application."""
    app.routes.append(Route(STREAM_PATH, stream_events, methods=["GET"]))
