"""Database-backed event bus for cross-replica live event fanout."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberLiveEvent
from caliber.events.bus import EventBus

logger = logging.getLogger("caliber.events.database_bus")

_PUBLISH_QUEUE_MAX = 512
_PERSIST_BATCH_MAX = 32
_POLL_BATCH_SIZE = 200
_POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class _StoredLiveEvent:
    event_id: int
    origin: str
    event_type: str
    payload: dict[str, Any]


class DatabaseEventBus(EventBus):
    """EventBus-compatible adapter that mirrors events through the database.

    Local subscribers still receive events immediately. When connected, the
    same event is appended to ``caliber_live_events``; peer instances tail
    those rows and forward remote events to their own local subscribers.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
        poll_batch_size: int = _POLL_BATCH_SIZE,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._origin = uuid.uuid4().hex
        self._poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._poll_batch_size = max(1, int(poll_batch_size))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._publish_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._publisher_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._last_seen_event_id = 0

    async def start(self) -> None:
        if self._publisher_task is not None or self._poll_task is not None:
            raise RuntimeError("DatabaseEventBus.start() called while already running")

        self._loop = asyncio.get_running_loop()
        self._publish_queue = asyncio.Queue(maxsize=_PUBLISH_QUEUE_MAX)
        try:
            self._last_seen_event_id = await asyncio.to_thread(self._current_max_event_id)
        except SQLAlchemyError as exc:
            raise RuntimeError(
                "database event backend requires the caliber_live_events table; run migrations first"
            ) from exc
        self._publisher_task = asyncio.create_task(
            self._publish_loop(),
            name="caliber.database_event_bus.publisher",
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="caliber.database_event_bus.poller",
        )
        logger.info("database event bus connected")

    async def stop(self) -> None:
        queue = self._publish_queue
        if queue is not None and self._publisher_task is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(queue.join(), timeout=1.0)

        tasks = [task for task in (self._publisher_task, self._poll_task) if task is not None]
        self._publisher_task = None
        self._poll_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._publish_queue = None
        self._loop = None
        logger.info("database event bus disconnected")

    def publish(self, event: dict[str, Any]) -> None:
        EventBus.publish(self, event)
        self._publish_remote(event)

    def _publish_remote(self, event: dict[str, Any]) -> None:
        if self._loop is None or self._publish_queue is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue_publish, dict(event))
        except RuntimeError:
            logger.debug(
                "database event bus loop closed; dropping remote event type=%r",
                event.get("type"),
            )

    def _enqueue_publish(self, event: dict[str, Any]) -> None:
        queue = self._publish_queue
        if queue is None:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "database event bus queue full; dropping event type=%r",
                event.get("type"),
            )

    async def _publish_loop(self) -> None:
        queue = self._publish_queue
        if queue is None:
            return
        while True:
            event = await queue.get()
            batch = [event]
            while len(batch) < _PERSIST_BATCH_MAX:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await asyncio.to_thread(self._persist_events, batch)
            except Exception as exc:
                logger.warning("failed to persist event batch to database: %s", exc)
            finally:
                for _ in batch:
                    queue.task_done()

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("database event bus poll failed", exc_info=True)
            await asyncio.sleep(self._poll_interval_seconds)

    async def _poll_once(self) -> None:
        while True:
            rows = await asyncio.to_thread(
                self._fetch_rows_after,
                self._last_seen_event_id,
                self._poll_batch_size,
            )
            if not rows:
                return
            self._last_seen_event_id = rows[-1].event_id
            for row in rows:
                if row.origin == self._origin:
                    continue
                payload = dict(row.payload)
                if "type" not in payload and row.event_type != "message":
                    payload["type"] = row.event_type
                payload["_caliber_remote"] = True
                EventBus.publish(self, payload)
            if len(rows) < self._poll_batch_size:
                return

    def _current_max_event_id(self) -> int:
        with self._session_factory() as session:
            current = session.execute(select(func.max(CaliberLiveEvent.event_id))).scalar_one()
        return int(current or 0)

    def _persist_events(self, events: Sequence[dict[str, Any]]) -> None:
        rows = [
            CaliberLiveEvent(
                origin=self._origin,
                event_type=str(event.get("type") or "message"),
                payload=_json_safe_event(event),
            )
            for event in events
        ]
        with self._session_factory() as session:
            session.add_all(rows)
            session.commit()

    def _fetch_rows_after(self, last_seen_event_id: int, limit: int) -> list[_StoredLiveEvent]:
        stmt = (
            select(
                CaliberLiveEvent.event_id,
                CaliberLiveEvent.origin,
                CaliberLiveEvent.event_type,
                CaliberLiveEvent.payload,
            )
            .where(CaliberLiveEvent.event_id > last_seen_event_id)
            .order_by(CaliberLiveEvent.event_id.asc())
            .limit(limit)
        )
        with self._session_factory() as session:
            rows = session.execute(stmt).all()
        return [
            _StoredLiveEvent(
                event_id=int(event_id),
                origin=str(origin),
                event_type=str(event_type or "message"),
                payload=dict(payload) if isinstance(payload, dict) else {},
            )
            for event_id, origin, event_type, payload in rows
        ]


def _json_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(dict(event), default=str))
    if isinstance(payload, dict):
        return payload
    return {"value": payload}
