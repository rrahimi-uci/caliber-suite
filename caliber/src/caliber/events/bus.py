"""Base event bus for the SSE stream.

A minimal pub/sub primitive that routes share via ``app.state.event_bus``.
Endpoints that mutate state (approve, verify, worker stage transitions)
call :meth:`EventBus.publish` synchronously with a small dict payload;
the SSE endpoint subscribes and forwards each event to the connected
client.

This module implements the in-memory fan-out primitive that every backend
builds on. Adapters like the database-backed and NATS-backed buses mirror
the same events across replicas, but they still forward into this local
subscriber set so SSE clients keep a single consumption path.

The publish path is sync (it just puts each event onto every subscriber's
asyncio queue) so callers from sync code paths — the orchestrator
worker, route handlers in the middle of a DB transaction — can fire
events without restructuring around ``await``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("caliber.events.bus")

# Bounded subscriber queues protect against a slow consumer accumulating
# unbounded memory. When the queue fills the producer drops new events for
# that subscriber; the SSE client will see a missed-event gap rather than
# stall the whole bus.
_SUBSCRIBER_QUEUE_MAX = 128


class EventBus:
    """In-memory fan-out queue."""

    def __init__(self) -> None:
        # Each subscriber is the (queue, loop) pair captured at subscribe
        # time. ``publish`` is sync and is called from arbitrary threads
        # (the worker dispatches its body via ``asyncio.to_thread``), so
        # we must hop back to the queue's owning loop before touching it
        # — ``asyncio.Queue.put_nowait`` is not thread-safe.
        self._subscribers: set[tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]] = (
            set()
        )

    def subscriber_count(self) -> int:
        """Return the number of active subscribers. Useful for tests + metrics."""
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan an event out to every current subscriber.

        Sync entry point so the worker and route handlers can publish
        without restructuring around ``await``. If a subscriber's queue
        is full we drop the event for *that* subscriber only — the rest
        still see it.

        Safe to call from any thread: we use ``call_soon_threadsafe`` to
        enqueue on the subscriber's owning loop rather than touching the
        ``asyncio.Queue`` directly.
        """
        if not self._subscribers:
            return
        for queue, loop in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(_safe_put, queue, event)
            except RuntimeError:
                # Loop was closed between subscribe and publish — drop
                # the subscription on the next subscribe iteration's
                # cleanup; nothing else to do here.
                logger.debug(
                    "event bus subscriber loop closed; dropping event of type=%r",
                    event.get("type"),
                )

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events for one subscriber.

        The async iterator is intentionally unbounded — the SSE endpoint
        cancels its task when the client disconnects, and the ``finally``
        block here removes the queue from the subscriber set so the bus
        doesn't leak.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        loop = asyncio.get_running_loop()
        entry = (queue, loop)
        self._subscribers.add(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(entry)


def _safe_put(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    """Enqueue ``event`` onto ``queue`` from the queue's owning loop.

    Scheduled via ``call_soon_threadsafe`` from :meth:`EventBus.publish`.
    Bounded-queue overflow is downgraded to a warning so a slow SSE
    consumer can't break unrelated subscribers.
    """
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "event bus subscriber queue full; dropping event of type=%r",
            event.get("type"),
        )
