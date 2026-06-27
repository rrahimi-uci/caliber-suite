"""Unit tests for :class:`caliber.events.bus.EventBus`."""

from __future__ import annotations

import asyncio

import pytest

from caliber.events.bus import EventBus


@pytest.mark.asyncio
async def test_publish_to_no_subscribers_is_noop() -> None:
    bus = EventBus()
    bus.publish({"type": "test"})  # must not raise
    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
async def test_subscriber_receives_published_events() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    # Prime the iterator so the bus actually has a subscriber.
    next_task = asyncio.create_task(subscription.__anext__())
    # Yield so the bus registers the queue.
    await asyncio.sleep(0)
    assert bus.subscriber_count() == 1

    bus.publish({"type": "ping", "n": 1})
    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {"type": "ping", "n": 1}

    await subscription.aclose()


@pytest.mark.asyncio
async def test_multiple_subscribers_each_see_event() -> None:
    bus = EventBus()
    sub_a = bus.subscribe()
    sub_b = bus.subscribe()
    task_a = asyncio.create_task(sub_a.__anext__())
    task_b = asyncio.create_task(sub_b.__anext__())
    await asyncio.sleep(0)

    bus.publish({"type": "fan-out"})
    a = await asyncio.wait_for(task_a, timeout=1.0)
    b = await asyncio.wait_for(task_b, timeout=1.0)
    assert a == {"type": "fan-out"}
    assert b == {"type": "fan-out"}

    await sub_a.aclose()
    await sub_b.aclose()


@pytest.mark.asyncio
async def test_subscription_cleanup_removes_queue_from_bus() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    # Advance the generator past its setup so the queue is registered.
    task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)
    assert bus.subscriber_count() == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await subscription.aclose()
    assert bus.subscriber_count() == 0
