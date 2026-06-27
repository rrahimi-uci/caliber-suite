"""Unit tests for the Redis-backed event bus adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from caliber.events.redis_bus import RedisEventBus


class _FakePubSub:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.subscribed_channels: list[str] = []
        self.unsubscribed_channels: list[str | None] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_channels.append(channel)

    def listen(self) -> Any:
        return self._messages()

    async def _messages(self) -> Any:
        while True:
            yield await self.queue.get()

    async def unsubscribe(self, channel: str | None = None) -> None:
        self.unsubscribed_channels.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedisConnection:
    def __init__(self) -> None:
        self.url: str | None = None
        self.ignore_subscribe_messages: bool | None = None
        self.pubsub_instance = _FakePubSub()
        self.published: list[tuple[str, str]] = []
        self.closed = False

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> _FakePubSub:
        self.ignore_subscribe_messages = ignore_subscribe_messages
        return self.pubsub_instance

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedisConnection:
    connection = _FakeRedisConnection()
    real_import_module = importlib.import_module

    def from_url(url: str) -> _FakeRedisConnection:
        connection.url = url
        return connection

    def fake_import_module(name: str, package: str | None = None) -> Any:
        if name == "redis.asyncio":
            return SimpleNamespace(from_url=from_url)
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    return connection


@pytest.mark.asyncio
async def test_redis_event_bus_publishes_local_and_remote(
    fake_redis: _FakeRedisConnection,
) -> None:
    bus = RedisEventBus(url="redis://cache:6379/0", channel="caliber.test")
    await bus.start()

    subscription = bus.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    bus.publish({"type": "workflow.run.queued", "workflow_run_id": "WR-1"})

    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}

    for _ in range(10):
        if fake_redis.published:
            break
        await asyncio.sleep(0.01)

    assert fake_redis.url == "redis://cache:6379/0"
    assert fake_redis.ignore_subscribe_messages is True
    assert fake_redis.pubsub_instance.subscribed_channels == ["caliber.test"]
    assert fake_redis.published
    channel, payload = fake_redis.published[0]
    assert channel == "caliber.test"
    envelope = json.loads(payload)
    assert envelope["event"] == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}

    await subscription.aclose()
    await bus.stop()
    assert fake_redis.pubsub_instance.unsubscribed_channels == ["caliber.test"]
    assert fake_redis.pubsub_instance.closed is True
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_redis_event_bus_forwards_remote_events(
    fake_redis: _FakeRedisConnection,
) -> None:
    bus = RedisEventBus(url="redis://cache:6379/0", channel="caliber.test")
    await bus.start()

    subscription = bus.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    await fake_redis.pubsub_instance.queue.put(
        {
            "data": json.dumps(
                {
                    "origin": "peer",
                    "event": {"type": "workflow.run.started", "workflow_run_id": "WR-2"},
                }
            ).encode("utf-8")
        }
    )

    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {
        "type": "workflow.run.started",
        "workflow_run_id": "WR-2",
        "_caliber_remote": True,
    }

    await subscription.aclose()
    await bus.stop()


def test_redis_event_bus_decodes_and_filters_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = RedisEventBus(url="  ", channel="  ")

    assert bus._url == ""
    assert bus._channel == "caliber.events"

    with caplog.at_level(logging.WARNING, logger="caliber.events.redis_bus"):
        assert bus._decode_message(b"{not-json") is None
    assert "dropping malformed Redis event payload" in caplog.text

    assert bus._decode_message(json.dumps(["not", "a", "dict"])) is None
    assert bus._decode_message(42) is None
    assert (
        bus._decode_message(
            json.dumps({"origin": bus._origin, "event": {"type": "workflow.run.queued"}}).encode(
                "utf-8"
            )
        )
        is None
    )
    assert bus._decode_message(json.dumps({"type": "legacy.event"})) == {
        "type": "legacy.event",
        "_caliber_remote": True,
    }


@pytest.mark.asyncio
async def test_redis_event_bus_start_guards_config_and_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="CALIBER_REDIS_URL"):
        await RedisEventBus(url=" , ", channel="caliber.test").start()

    real_import_module = importlib.import_module

    def fake_import_module(name: str, package: str | None = None) -> Any:
        if name == "redis.asyncio":
            raise ImportError("missing redis")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    with pytest.raises(RuntimeError, match="requires the 'redis' extra"):
        await RedisEventBus(url="redis://cache:6379/0", channel="caliber.test").start()


@pytest.mark.asyncio
async def test_redis_event_bus_start_stop_guards_and_suppresses_cleanup(
    fake_redis: _FakeRedisConnection,
) -> None:
    async def fail_unsubscribe(channel: str | None = None) -> None:
        raise RuntimeError(f"unsubscribe failed for {channel}")

    async def fail_close() -> None:
        raise RuntimeError("close failed")

    bus = RedisEventBus(url="redis://cache:6379/0", channel="caliber.test")
    await bus.start()

    with pytest.raises(RuntimeError, match="already running"):
        await bus.start()

    fake_redis.pubsub_instance.unsubscribe = fail_unsubscribe  # type: ignore[method-assign]
    fake_redis.pubsub_instance.aclose = fail_close  # type: ignore[method-assign]
    fake_redis.aclose = fail_close  # type: ignore[method-assign]
    await bus.stop()
    await bus.stop()

    assert bus._pubsub is None
    assert bus._redis is None
    assert bus._loop is None


class _ClosedLoop:
    def is_closed(self) -> bool:
        return True

    def call_soon_threadsafe(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("closed loops should short-circuit before scheduling")


class _RaisingLoop:
    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("loop closed")


def test_redis_event_bus_publish_remote_handles_unavailable_loops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = RedisEventBus(url="redis://cache:6379/0", channel="caliber.test")

    bus._publish_remote({"type": "no.connection"})

    bus._loop = _ClosedLoop()  # type: ignore[assignment]
    bus._redis = object()
    bus._publish_remote({"type": "closed.loop"})

    bus._loop = _RaisingLoop()  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger="caliber.events.redis_bus"):
        bus._publish_remote({"type": "raises.loop"})
    assert "dropping remote event type='raises.loop'" in caplog.text


@pytest.mark.asyncio
async def test_redis_event_bus_publish_task_failures_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BadConnection:
        async def publish(self, channel: str, payload: str) -> None:
            assert channel == "caliber.test"
            assert '"type":"boom"' in payload
            raise RuntimeError("redis down")

    bus = RedisEventBus(url="redis://cache:6379/0", channel="caliber.test")
    await bus._publish_to_redis({"type": "noop"})

    bus._redis = BadConnection()
    with caplog.at_level(logging.WARNING, logger="caliber.events.redis_bus"):
        bus._schedule_publish({"type": "boom"})
        for _ in range(10):
            if "failed to publish event to Redis: redis down" in caplog.text:
                break
            await asyncio.sleep(0)

    assert "failed to publish event to Redis: redis down" in caplog.text
    assert bus._publish_tasks == set()
