"""Unit tests for the NATS-backed event bus adapter."""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from caliber.config import CaliberConfig
from caliber.events.bus import EventBus
from caliber.events.nats_bus import NatsEventBus, build_event_bus
from caliber.events.redis_bus import RedisEventBus


class _FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakeSubscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[_FakeMsg] = asyncio.Queue()
        self.unsubscribed = False

    @property
    def messages(self) -> Any:
        return self._messages()

    async def _messages(self) -> Any:
        while True:
            yield await self.queue.get()

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeConnection:
    def __init__(self) -> None:
        self.subscription = _FakeSubscription()
        self.published: list[tuple[str, bytes]] = []
        self.servers: list[str] = []
        self.closed = False

    async def subscribe(self, subject: str) -> _FakeSubscription:
        self.subject = subject
        return self.subscription

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def drain(self) -> None:
        self.closed = True


@pytest.fixture
def fake_nats(monkeypatch: pytest.MonkeyPatch) -> _FakeConnection:
    conn = _FakeConnection()

    async def connect(*, servers: list[str]) -> _FakeConnection:
        conn.servers = servers
        return conn

    monkeypatch.setitem(sys.modules, "nats", SimpleNamespace(connect=connect))
    return conn


@pytest.mark.asyncio
async def test_nats_event_bus_publishes_local_and_remote(fake_nats: _FakeConnection) -> None:
    bus = NatsEventBus(url="nats://example:4222", subject="caliber.test")
    await bus.start()

    subscription = bus.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    bus.publish({"type": "workflow.run.queued", "workflow_run_id": "WR-1"})

    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}

    for _ in range(10):
        if fake_nats.published:
            break
        await asyncio.sleep(0.01)
    assert fake_nats.servers == ["nats://example:4222"]
    assert fake_nats.published
    subject, payload = fake_nats.published[0]
    assert subject == "caliber.test"
    envelope = json.loads(payload.decode("utf-8"))
    assert envelope["event"] == {"type": "workflow.run.queued", "workflow_run_id": "WR-1"}

    await subscription.aclose()
    await bus.stop()
    assert fake_nats.subscription.unsubscribed is True
    assert fake_nats.closed is True


@pytest.mark.asyncio
async def test_nats_event_bus_forwards_remote_events(fake_nats: _FakeConnection) -> None:
    bus = NatsEventBus(url="nats://example:4222", subject="caliber.test")
    await bus.start()

    subscription = bus.subscribe()
    next_task = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)

    await fake_nats.subscription.queue.put(
        _FakeMsg(
            json.dumps(
                {
                    "origin": "peer",
                    "event": {"type": "workflow.run.started", "workflow_run_id": "WR-2"},
                }
            ).encode("utf-8")
        )
    )

    event = await asyncio.wait_for(next_task, timeout=1.0)
    assert event == {
        "type": "workflow.run.started",
        "workflow_run_id": "WR-2",
        "_caliber_remote": True,
    }

    await subscription.aclose()
    await bus.stop()


def test_nats_event_bus_decodes_and_filters_payloads(caplog: pytest.LogCaptureFixture) -> None:
    bus = NatsEventBus(url="  ", subject="  ")

    assert bus._servers == []
    assert bus._subject == "caliber.events"

    with caplog.at_level(logging.WARNING, logger="caliber.events.nats_bus"):
        assert bus._decode_message(b"{not-json") is None
    assert "dropping malformed NATS event payload" in caplog.text

    assert bus._decode_message(json.dumps(["not", "a", "dict"]).encode("utf-8")) is None
    assert (
        bus._decode_message(
            json.dumps({"origin": bus._origin, "event": {"type": "workflow.run.queued"}}).encode(
                "utf-8"
            )
        )
        is None
    )
    assert bus._decode_message(json.dumps({"type": "legacy.event"}).encode("utf-8")) == {
        "type": "legacy.event",
        "_caliber_remote": True,
    }


@pytest.mark.asyncio
async def test_nats_event_bus_start_guards_config_and_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="CALIBER_NATS_URL"):
        await NatsEventBus(url=" , ", subject="caliber.test").start()

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "nats":
            raise ImportError("missing nats")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="requires the 'nats' extra"):
        await NatsEventBus(url="nats://example:4222", subject="caliber.test").start()


@pytest.mark.asyncio
async def test_nats_event_bus_start_stop_guards_and_suppresses_cleanup(
    fake_nats: _FakeConnection,
) -> None:
    async def fail_unsubscribe() -> None:
        raise RuntimeError("unsubscribe failed")

    async def fail_drain() -> None:
        raise RuntimeError("drain failed")

    bus = NatsEventBus(url="nats://example:4222", subject="caliber.test")
    await bus.start()

    with pytest.raises(RuntimeError, match="already running"):
        await bus.start()

    fake_nats.subscription.unsubscribe = fail_unsubscribe  # type: ignore[method-assign]
    fake_nats.drain = fail_drain  # type: ignore[method-assign]
    await bus.stop()
    await bus.stop()
    assert bus._sub is None
    assert bus._nc is None
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


def test_nats_event_bus_publish_remote_handles_unavailable_loops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = NatsEventBus(url="nats://example:4222", subject="caliber.test")

    bus._publish_remote({"type": "no.connection"})

    bus._loop = _ClosedLoop()  # type: ignore[assignment]
    bus._nc = object()
    bus._publish_remote({"type": "closed.loop"})

    bus._loop = _RaisingLoop()  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger="caliber.events.nats_bus"):
        bus._publish_remote({"type": "raises.loop"})
    assert "dropping remote event type='raises.loop'" in caplog.text


@pytest.mark.asyncio
async def test_nats_event_bus_publish_task_failures_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BadConnection:
        async def publish(self, subject: str, payload: bytes) -> None:
            assert subject == "caliber.test"
            assert b'"type":"boom"' in payload
            raise RuntimeError("nats down")

    bus = NatsEventBus(url="nats://example:4222", subject="caliber.test")
    await bus._publish_to_nats({"type": "noop"})

    bus._nc = BadConnection()
    with caplog.at_level(logging.WARNING, logger="caliber.events.nats_bus"):
        bus._schedule_publish({"type": "boom"})
        for _ in range(10):
            if "failed to publish event to NATS: nats down" in caplog.text:
                break
            await asyncio.sleep(0)

    assert "failed to publish event to NATS: nats down" in caplog.text
    assert bus._publish_tasks == set()


def test_build_event_bus_selects_backend() -> None:
    nats_bus = build_event_bus(
        CaliberConfig(
            workflow_run_event_backend="nats",
            nats_url="nats://example:4222",
            nats_subject="caliber.custom",
        )
    )
    assert isinstance(nats_bus, NatsEventBus)

    redis_bus = build_event_bus(
        CaliberConfig(
            workflow_run_event_backend="redis",
            redis_url="redis://cache:6379/7",
            redis_channel="caliber.events.live",
        )
    )
    assert isinstance(redis_bus, RedisEventBus)

    fallback = build_event_bus(CaliberConfig(workflow_run_event_backend="in_process"))
    assert isinstance(fallback, EventBus)
