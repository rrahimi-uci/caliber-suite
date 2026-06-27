"""Redis-backed event bus for cross-replica live event fanout."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import uuid
from contextlib import suppress
from typing import Any

from caliber.events.bus import EventBus

logger = logging.getLogger("caliber.events.redis_bus")


class RedisEventBus(EventBus):
    """EventBus-compatible adapter that mirrors events through Redis pub/sub.

    Local subscribers still receive events immediately. When connected, the
    same event is also published to a shared Redis channel; peer instances
    forward those remote events to their own local subscribers.
    """

    def __init__(self, *, url: str, channel: str) -> None:
        super().__init__()
        self._url = url.strip()
        self._channel = channel.strip() or "caliber.events"
        self._origin = uuid.uuid4().hex
        self._loop: asyncio.AbstractEventLoop | None = None
        self._redis: Any | None = None
        self._pubsub: Any | None = None
        self._consume_task: asyncio.Task[None] | None = None
        self._publish_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._consume_task is not None:
            raise RuntimeError("RedisEventBus.start() called while already running")
        if not self._url or not self._url.strip(","):
            raise RuntimeError("Redis event backend requires CALIBER_REDIS_URL")
        try:
            redis_asyncio = importlib.import_module("redis.asyncio")
        except ImportError as exc:
            raise RuntimeError(
                "Redis event backend requires the 'redis' extra "
                "(install caliber[redis] or use the CALIBER container image)"
            ) from exc

        self._redis = _redis_from_url(redis_asyncio, self._url)
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(self._channel)
        self._loop = asyncio.get_running_loop()
        self._consume_task = asyncio.create_task(
            self._consume(),
            name="caliber.redis_bus",
        )
        logger.info("Redis event bus connected (channel=%s)", self._channel)

    async def stop(self) -> None:
        task = self._consume_task
        self._consume_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        for publish_task in list(self._publish_tasks):
            publish_task.cancel()
        if self._publish_tasks:
            await asyncio.gather(*self._publish_tasks, return_exceptions=True)
            self._publish_tasks.clear()

        if self._pubsub is not None:
            unsubscribe = getattr(self._pubsub, "unsubscribe", None)
            if callable(unsubscribe):
                with suppress(Exception):
                    await _await_if_needed(unsubscribe(self._channel))
            close_pubsub = getattr(self._pubsub, "aclose", None) or getattr(
                self._pubsub,
                "close",
                None,
            )
            if callable(close_pubsub):
                with suppress(Exception):
                    await _await_if_needed(close_pubsub())
            self._pubsub = None

        if self._redis is not None:
            close_redis = getattr(self._redis, "aclose", None) or getattr(
                self._redis,
                "close",
                None,
            )
            if callable(close_redis):
                with suppress(Exception):
                    await _await_if_needed(close_redis())
            self._redis = None

        self._loop = None
        logger.info("Redis event bus disconnected")

    def publish(self, event: dict[str, Any]) -> None:
        EventBus.publish(self, event)
        self._publish_remote(event)

    async def _consume(self) -> None:
        if self._pubsub is None:
            return
        async for message in self._pubsub.listen():
            payload = message.get("data") if isinstance(message, dict) else message
            event = self._decode_message(payload)
            if event is not None:
                EventBus.publish(self, event)

    def _decode_message(self, payload: object) -> dict[str, Any] | None:
        body = _load_event_body(payload)
        if body is None:
            return None

        if body.get("origin") == self._origin:
            return None
        event = body.get("event")
        marked = dict(event) if isinstance(event, dict) else dict(body)
        marked["_caliber_remote"] = True
        return marked

    def _publish_remote(self, event: dict[str, Any]) -> None:
        if self._loop is None or self._redis is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._schedule_publish, dict(event))
        except RuntimeError:
            logger.debug(
                "Redis event bus loop closed; dropping remote event type=%r",
                event.get("type"),
            )

    def _schedule_publish(self, event: dict[str, Any]) -> None:
        task = asyncio.create_task(self._publish_to_redis(event))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_done)

    def _publish_done(self, task: asyncio.Task[None]) -> None:
        self._publish_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("failed to publish event to Redis: %s", exc)

    async def _publish_to_redis(self, event: dict[str, Any]) -> None:
        if self._redis is None:
            return
        payload = json.dumps(
            {"origin": self._origin, "event": event},
            separators=(",", ":"),
            default=str,
        )
        await self._redis.publish(self._channel, payload)


def _redis_from_url(redis_asyncio: Any, url: str) -> Any:
    from_url = getattr(redis_asyncio, "from_url", None)
    if callable(from_url):
        return from_url(url)

    redis_cls = getattr(redis_asyncio, "Redis", None)
    if redis_cls is None or not hasattr(redis_cls, "from_url"):
        raise RuntimeError("redis.asyncio client does not expose from_url()")
    return redis_cls.from_url(url)


async def _await_if_needed(result: object) -> None:
    if inspect.isawaitable(result):
        await result


def _load_event_body(payload: object) -> dict[str, Any] | None:
    raw_payload: str | None = None
    if isinstance(payload, memoryview):
        raw_payload = payload.tobytes().decode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        raw_payload = bytes(payload).decode("utf-8")
    elif isinstance(payload, str):
        raw_payload = payload
    if raw_payload is None:
        return None

    try:
        body = json.loads(raw_payload)
    except Exception:
        logger.warning("dropping malformed Redis event payload")
        return None
    return body if isinstance(body, dict) else None
