"""NATS-backed event bus for cross-replica live event fanout."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from caliber.config import CaliberConfig
from caliber.events.bus import EventBus
from caliber.events.database_bus import DatabaseEventBus
from caliber.events.redis_bus import RedisEventBus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("caliber.events.nats_bus")


class NatsEventBus(EventBus):
    """EventBus-compatible adapter that mirrors events through NATS.

    Local subscribers still receive events immediately. When connected, the
    same event is also published to a shared NATS subject; peer instances forward
    those remote events to their own local subscribers.
    """

    def __init__(self, *, url: str, subject: str) -> None:
        super().__init__()
        self._servers = [part.strip() for part in url.split(",") if part.strip()]
        self._subject = subject.strip() or "caliber.events"
        self._origin = uuid.uuid4().hex
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nc: Any | None = None
        self._sub: Any | None = None
        self._consume_task: asyncio.Task[None] | None = None
        self._publish_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._consume_task is not None:
            raise RuntimeError("NatsEventBus.start() called while already running")
        if not self._servers:
            raise RuntimeError("NATS event backend requires CALIBER_NATS_URL")
        try:
            import nats  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "NATS event backend requires the 'nats' extra "
                "(install caliber-suite[nats] or use the CALIBER container image)"
            ) from exc

        self._loop = asyncio.get_running_loop()
        self._nc = await nats.connect(servers=self._servers)
        self._sub = await self._nc.subscribe(self._subject)
        self._consume_task = asyncio.create_task(self._consume(), name="caliber.nats_bus")
        logger.info(
            "NATS event bus connected (servers=%s subject=%s)", self._servers, self._subject
        )

    async def stop(self) -> None:
        task = self._consume_task
        self._consume_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        for task in list(self._publish_tasks):
            task.cancel()
        if self._publish_tasks:
            await asyncio.gather(*self._publish_tasks, return_exceptions=True)
            self._publish_tasks.clear()

        if self._sub is not None:
            with suppress(Exception):
                await self._sub.unsubscribe()
            self._sub = None
        if self._nc is not None:
            with suppress(Exception):
                await self._nc.drain()
            self._nc = None
        self._loop = None
        logger.info("NATS event bus disconnected")

    def publish(self, event: dict[str, Any]) -> None:
        EventBus.publish(self, event)
        self._publish_remote(event)

    async def _consume(self) -> None:
        if self._sub is None:
            return
        async for msg in self._sub.messages:
            event = self._decode_message(getattr(msg, "data", b""))
            if event is not None:
                EventBus.publish(self, event)

    def _decode_message(self, payload: bytes) -> dict[str, Any] | None:
        try:
            body = json.loads(payload.decode("utf-8"))
        except Exception:
            logger.warning("dropping malformed NATS event payload")
            return None
        if not isinstance(body, dict):
            return None
        if body.get("origin") == self._origin:
            return None
        event = body.get("event")
        marked = dict(event) if isinstance(event, dict) else dict(body)
        marked["_caliber_remote"] = True
        return marked

    def _publish_remote(self, event: dict[str, Any]) -> None:
        if self._loop is None or self._nc is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._schedule_publish, dict(event))
        except RuntimeError:
            logger.debug(
                "NATS event bus loop closed; dropping remote event type=%r", event.get("type")
            )

    def _schedule_publish(self, event: dict[str, Any]) -> None:
        task = asyncio.create_task(self._publish_to_nats(event))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_done)

    def _publish_done(self, task: asyncio.Task[None]) -> None:
        self._publish_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("failed to publish event to NATS: %s", exc)

    async def _publish_to_nats(self, event: dict[str, Any]) -> None:
        if self._nc is None:
            return
        payload = json.dumps(
            {"origin": self._origin, "event": event},
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        await self._nc.publish(self._subject, payload)


def build_event_bus(
    config: CaliberConfig,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> EventBus:
    backend = config.workflow_run_event_backend
    if backend == "nats":
        return NatsEventBus(url=config.nats_url, subject=config.nats_subject)
    if backend == "database":
        if session_factory is None:
            raise ValueError("database event backend requires a session_factory")
        return DatabaseEventBus(session_factory=session_factory)
    if backend == "redis":
        return RedisEventBus(
            url=config.redis_url,
            channel=config.redis_channel,
        )
    return EventBus()
