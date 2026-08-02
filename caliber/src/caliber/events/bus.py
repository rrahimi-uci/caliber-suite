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
from collections.abc import AsyncIterator, Callable
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
        # The third element is an optional overflow reporter — see ``subscribe``.
        self._subscribers: set[
            tuple[
                asyncio.Queue[dict[str, Any]],
                asyncio.AbstractEventLoop,
                Callable[[dict[str, Any]], None] | None,
            ]
        ] = set()

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
        for queue, loop, on_drop in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(_safe_put, queue, event, on_drop)
            except RuntimeError:
                # Loop was closed between subscribe and publish — drop
                # the subscription on the next subscribe iteration's
                # cleanup; nothing else to do here.
                logger.debug(
                    "event bus subscriber loop closed; dropping event of type=%r",
                    event.get("type"),
                )

    async def subscribe(
        self, *, on_drop: Callable[[dict[str, Any]], None] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events for one subscriber.

        The async iterator is intentionally unbounded — the SSE endpoint
        cancels its task when the client disconnects, and the ``finally``
        block here removes the queue from the subscriber set so the bus
        doesn't leak.

        ``on_drop`` is invoked with any event this subscriber's bounded queue
        could not accept. A UI stream can ignore an overflow — the next frame
        supersedes it — but a subscriber that promises delivery cannot, and
        needs the drop as a record rather than a log line.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        loop = asyncio.get_running_loop()
        entry = (queue, loop, on_drop)
        self._subscribers.add(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(entry)


def _safe_put(
    queue: asyncio.Queue[dict[str, Any]],
    event: dict[str, Any],
    on_drop: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Enqueue ``event`` onto ``queue`` from the queue's owning loop.

    Scheduled via ``call_soon_threadsafe`` from :meth:`EventBus.publish`.
    Bounded-queue overflow does not break unrelated subscribers — but a warning
    in a log is not a record. A subscriber that needs to know it lost something
    (the webhook dispatcher, whose whole contract is "we will tell you") passes
    ``on_drop`` and turns the drop into a durable dead letter.

    ``on_drop`` failures are swallowed: an overflow handler that raised would
    take down the publish path it was added to make observable.
    """
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "event bus subscriber queue full; dropping event of type=%r",
            event.get("type"),
        )
        if on_drop is not None:
            try:
                on_drop(event)
            except Exception:
                logger.exception("event bus drop handler failed")
