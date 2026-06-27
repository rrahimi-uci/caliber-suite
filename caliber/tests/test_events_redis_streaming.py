"""App-boundary tests for Redis-backed live event streaming."""

from __future__ import annotations

import asyncio
import importlib
import json
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.events.redis_bus import RedisEventBus
from caliber.routes.events_stream import _event_loop
from caliber.server import create_app


class _FakeRedisBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[_FakePubSub]] = {}

    def subscribe(self, channel: str, pubsub: _FakePubSub) -> None:
        subscribers = self._subscribers.setdefault(channel, [])
        if pubsub not in subscribers:
            subscribers.append(pubsub)

    def unsubscribe(self, channel: str, pubsub: _FakePubSub) -> None:
        subscribers = self._subscribers.get(channel)
        if not subscribers:
            return
        with suppress(ValueError):
            subscribers.remove(pubsub)
        if not subscribers:
            self._subscribers.pop(channel, None)

    async def publish(self, channel: str, payload: str) -> None:
        for subscriber in list(self._subscribers.get(channel, [])):
            await subscriber.push(channel, payload)


class _FakePubSub:
    def __init__(self, broker: _FakeRedisBroker) -> None:
        self._broker = broker
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self._channels: set[str] = set()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._broker.subscribe(channel, self)

    def listen(self) -> Any:
        return self._messages()

    async def _messages(self) -> Any:
        while True:
            yield await self._queue.get()

    async def push(self, channel: str, payload: str) -> None:
        await self._queue.put({"channel": channel, "data": payload})

    async def unsubscribe(self, channel: str | None = None) -> None:
        channels = [channel] if channel is not None else list(self._channels)
        for channel_name in channels:
            if channel_name is None:
                continue
            self._broker.unsubscribe(channel_name, self)
            self._channels.discard(channel_name)

    async def aclose(self) -> None:
        await self.unsubscribe()
        self.closed = True


class _FakeRedisConnection:
    def __init__(self, broker: _FakeRedisBroker, url: str) -> None:
        self._broker = broker
        self.url = url
        self.pubsubs: list[_FakePubSub] = []
        self.published: list[tuple[str, str]] = []
        self.closed = False

    def pubsub(self, *, ignore_subscribe_messages: bool = False) -> _FakePubSub:
        del ignore_subscribe_messages
        pubsub = _FakePubSub(self._broker)
        self.pubsubs.append(pubsub)
        return pubsub

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))
        await self._broker.publish(channel, payload)

    async def aclose(self) -> None:
        self.closed = True


class _NeverDisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture
def fake_redis_asyncio(monkeypatch: pytest.MonkeyPatch) -> _FakeRedisBroker:
    broker = _FakeRedisBroker()
    real_import_module = importlib.import_module

    def from_url(url: str) -> _FakeRedisConnection:
        return _FakeRedisConnection(broker, url)

    def fake_import_module(name: str, package: str | None = None) -> Any:
        if name == "redis.asyncio":
            return SimpleNamespace(from_url=from_url)
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    return broker


def _redis_app_config(app_config: CaliberConfig) -> CaliberConfig:
    return app_config.model_copy(
        update={
            "workflow_run_event_backend": "redis",
            "redis_url": "redis://shared-broker:6379/0",
            "redis_channel": "caliber.events",
            "background_tasks_enabled": False,
        }
    )


def test_create_app_lifespan_starts_and_stops_redis_event_bus(
    app_config: CaliberConfig,
    fake_redis_asyncio: _FakeRedisBroker,
) -> None:
    del fake_redis_asyncio
    app = create_app(config=_redis_app_config(app_config))
    bus = app.state.event_bus
    assert isinstance(bus, RedisEventBus)
    assert bus._consume_task is None

    with TestClient(app):
        assert bus._consume_task is not None
        assert bus._redis is not None
        assert bus._pubsub is not None

    assert bus._consume_task is None
    assert bus._redis is None
    assert bus._pubsub is None


@pytest.mark.asyncio
async def test_redis_backed_apps_forward_remote_events_into_sse_loop(
    app_config: CaliberConfig,
    fake_redis_asyncio: _FakeRedisBroker,
) -> None:
    del fake_redis_asyncio
    config = _redis_app_config(app_config)
    app_a = create_app(config=config)
    app_b = create_app(config=config)
    bus_a = app_a.state.event_bus
    bus_b = app_b.state.event_bus
    assert isinstance(bus_a, RedisEventBus)
    assert isinstance(bus_b, RedisEventBus)

    await bus_a.start()
    await bus_b.start()
    stream = _event_loop(_NeverDisconnectedRequest(), bus_a)
    try:
        connected_frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        assert connected_frame.startswith(b"event: connected\n")

        next_frame_task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        bus_b.publish({"type": "workflow.run.queued", "workflow_run_id": "WR-redis"})
        frame = await asyncio.wait_for(next_frame_task, timeout=1.0)
        text = frame.decode("utf-8")
        assert text.startswith("event: workflow.run.queued\n")
        payload_line = next(line for line in text.split("\n") if line.startswith("data: "))
        payload = json.loads(payload_line[len("data: ") :])
        assert payload == {
            "type": "workflow.run.queued",
            "workflow_run_id": "WR-redis",
        }
    finally:
        await stream.aclose()
        await bus_b.stop()
        await bus_a.stop()
