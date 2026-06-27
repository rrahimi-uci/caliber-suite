"""Extended tests for :class:`caliber.events.bus.EventBus` edge cases.

Covers the RuntimeError (closed loop) and QueueFull (slow consumer) branches
that the base test suite doesn't exercise.
"""

from __future__ import annotations

import asyncio

import pytest

from caliber.events.bus import _SUBSCRIBER_QUEUE_MAX, EventBus, _safe_put


@pytest.mark.asyncio
async def test_publish_to_closed_loop_does_not_crash() -> None:
    """When a subscriber's event loop is closed, publish drops the event."""
    bus = EventBus()
    # Create a subscriber in a secondary loop, then close that loop.
    secondary_loop = asyncio.new_event_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
    bus._subscribers.add((queue, secondary_loop))
    secondary_loop.close()

    # Should not raise even though the loop is closed.
    bus.publish({"type": "test"})

    # Clean up.
    bus._subscribers.clear()


@pytest.mark.asyncio
async def test_safe_put_drops_event_when_queue_full() -> None:
    """When a subscriber queue is at capacity, the event is dropped (not raised)."""
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    await queue.put({"type": "fill"})

    # Should not raise — just log a warning and drop.
    _safe_put(queue, {"type": "dropped"})

    # Queue still has only the first event.
    assert queue.qsize() == 1
    item = queue.get_nowait()
    assert item == {"type": "fill"}


@pytest.mark.asyncio
async def test_publish_fan_out_with_one_full_subscriber() -> None:
    """A full subscriber doesn't block delivery to other subscribers."""
    bus = EventBus()
    loop = asyncio.get_running_loop()

    # Subscriber A: will be full.
    queue_a: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    await queue_a.put({"type": "fill"})
    bus._subscribers.add((queue_a, loop))

    # Subscriber B: normal.
    sub_b = bus.subscribe()
    task_b = asyncio.create_task(sub_b.__anext__())
    await asyncio.sleep(0)

    bus.publish({"type": "new_event"})

    event = await asyncio.wait_for(task_b, timeout=1.0)
    assert event == {"type": "new_event"}

    # A's queue still has just the fill event.
    assert queue_a.qsize() == 1

    await sub_b.aclose()
    bus._subscribers.clear()
