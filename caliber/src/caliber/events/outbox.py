"""The transactional outbox and the consumer inbox: business events that survive crashes (B13.4, #685; ADR 0002).

A business transition and the event that announces it must stand or fall together. Publishing to a queue after the
commit can lose the event (the process dies in between) and publishing before it can announce a transition that never
happened. The outbox removes the gap:

1. :func:`enqueue` writes an :class:`~caliber.db.models.CaliberEventOutbox` row **in the transition's own
   transaction** (``workflows/run_events.py::append_run_event`` does so for every run event). Rolled back, the
   transition and its event vanish together; committed, both exist.
2. :class:`OutboxPublisher` (an owner-only loop) claims due rows and hands each to every registered consumer. A
   consumer's ``receive`` returns only when it has durably accepted the event: for the SQL consumer
   (:class:`SqlInboxConsumer`) that means a committed row in ``caliber_event_inbox`` keyed ``(consumer, event_id)``.
3. Only after **every** handoff was acknowledged is the row marked ``delivered``. A failure schedules a retry with
   exponential backoff; a row that exhausts its attempts becomes ``dead`` and stays for an operator.

Guarantees and their limits, stated plainly:

* **At-least-once.** A crash before the handoff, after the handoff but before the mark, or during consumption
  redelivers. The event id is stable (derived from the transition, so a retried transition cannot mint a second
  event) and is the deduplication key; **deduplication is the consumer's**: the inbox's primary key makes redelivery a
  no-op.
* **No business event is lost to a full notification queue.** SSE and the in-process bus remain a *notification*
  (a slow subscriber's full queue drops its copy); the outbox row is the durable truth and is delivered regardless.
* **Not ordered across events.** Rows are claimed oldest first but retries and concurrent publishers reorder; a
  consumer that needs order uses the aggregate's own sequence (a run event's ``sequence``).
* **Not exactly-once to the outside world.** An external consumer must deduplicate on ``event_id``.
* The envelope is an ADR 0003 ``event_envelope`` v1 record: the payload is *referenced* (``dataref``, ``datahash``),
  never inlined; it is kept in the row's ``payload`` and handed over beside the envelope.

Delivery is switched on with ``CALIBER_EVENT_OUTBOX_ENABLED`` (always on in the reference profile); off, nothing is
written and the publisher is not started. With no consumer registered a row is delivered at once (there is nobody to
hand it to) and pruned after the retention window, so the table stays bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Protocol

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.contracts import validate_record
from caliber.db.models import CaliberEventInbox, CaliberEventOutbox
from caliber.db.transactions import begin_sqlite_transaction

logger = logging.getLogger("caliber.events.outbox")

SOURCE: Final = "caliber"
TENANT: Final = "local"
PENDING: Final = "pending"
DELIVERED: Final = "delivered"
DEAD: Final = "dead"
_MAX_ERROR = 1000

_state = {"enabled": False}
_state_lock = threading.Lock()


def bind_outbox(enabled: bool) -> None:
    """Switch the outbox on or off for this process (bound once at startup, like the other process-wide services)."""
    with _state_lock:
        _state["enabled"] = bool(enabled)


def outbox_enabled() -> bool:
    return _state["enabled"]


def outbox_active(config: object | None) -> bool:
    """Whether a deployment records and delivers outbox events: the reference profile, or the explicit flag."""
    return getattr(config, "profile", "") == "reference" or bool(
        getattr(config, "event_outbox_enabled", False)
    )


class OutboxConflict(ValueError):  # noqa: N818 - a conflict, named for what it says
    """The same event id was enqueued twice with different content."""


def _utc(moment: datetime | None) -> datetime:
    """A timezone-aware UTC instant from ``moment`` (naive values are taken as UTC), or now."""
    if moment is None:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _naive(moment: datetime) -> datetime:
    return _utc(moment).astimezone(timezone.utc).replace(tzinfo=None)


def _rfc3339(moment: datetime) -> str:
    return (
        _utc(moment)
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def payload_hash(payload: dict[str, Any]) -> str:
    """``sha256:<hex>`` of the payload's canonical JSON (sorted keys, compact): the envelope's ``datahash``."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_envelope(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    time: datetime,
    correlation_id: str,
    causation_id: str | None = None,
    tenant_id: str = TENANT,
) -> dict[str, Any]:
    """The ADR 0003 ``event_envelope`` v1 record for one event; raises ``ContractValidationError`` if it is not one."""
    envelope: dict[str, Any] = {
        "specversion": "1.0",
        "id": event_id,
        "source": SOURCE,
        "type": event_type,
        "time": _rfc3339(time),
        "tenantid": tenant_id,
        "correlationid": correlation_id,
        "causationid": causation_id,
        "dataref": f"caliber://event_outbox/{event_id}",
        "datahash": payload_hash(payload),
        "schemaversion": "v1",
    }
    validate_record("event_envelope", envelope)
    return envelope


def enqueue(
    session: Session,
    *,
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    project_id: str | None,
    payload: dict[str, Any],
    correlation_id: str | None = None,
    causation_id: str | None = None,
    now: datetime | None = None,
) -> CaliberEventOutbox:
    """Record one event in the caller's transaction (flushes, never commits).

    Idempotent on ``event_id``: enqueuing the same event again with the same content returns the existing row;
    with different content raises :class:`OutboxConflict`. The row commits or rolls back with the transition.
    """
    moment = _utc(now)
    envelope = build_envelope(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        time=moment,
        correlation_id=correlation_id or aggregate_id,
        causation_id=causation_id,
    )
    row = CaliberEventOutbox(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        project_id=project_id,
        envelope=envelope,
        payload=payload,
        status=PENDING,
        attempts=0,
        next_attempt_at=_naive(moment),
        created_at=_naive(moment),
    )
    begin_sqlite_transaction(session)
    existing = session.get(CaliberEventOutbox, event_id)
    if existing is None:
        try:
            with session.begin_nested():
                session.add(row)
        except IntegrityError:  # another transaction recorded it between the read and the insert
            existing = session.get(CaliberEventOutbox, event_id, populate_existing=True)
            if (
                existing is None
            ):  # pragma: no cover - the conflicting row vanished between the statements
                raise
        else:
            return row
    if existing.envelope.get("datahash") != envelope["datahash"]:
        raise OutboxConflict(f"event {event_id!r} was already recorded with different content")
    return existing


# ---------------------------------------------------------------- consumers and the inbox


def receive(
    session: Session, consumer: str, envelope: dict[str, Any], payload: dict[str, Any]
) -> bool:
    """Record that ``consumer`` has the event; ``True`` if it is new, ``False`` if it already had it (flushes only)."""
    row = CaliberEventInbox(
        consumer=consumer,
        event_id=str(envelope["id"]),
        event_type=str(envelope["type"]),
        envelope=envelope,
        payload=payload,
    )
    begin_sqlite_transaction(session)
    if session.get(CaliberEventInbox, (consumer, str(envelope["id"]))) is not None:
        return False
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:  # another transaction recorded it between the read and the insert
        return False
    return True


def claim_unprocessed(
    session: Session, consumer: str, *, limit: int = 100
) -> list[CaliberEventInbox]:
    """The consumer's received but not yet processed events, oldest first."""
    return list(
        session.execute(
            select(CaliberEventInbox)
            .where(CaliberEventInbox.consumer == consumer, CaliberEventInbox.processed_at.is_(None))
            .order_by(CaliberEventInbox.received_at, CaliberEventInbox.event_id)
            .limit(limit)
        ).scalars()
    )


def mark_processed(
    session: Session, consumer: str, event_id: str, *, now: datetime | None = None
) -> bool:
    """Mark one inbox event processed; ``False`` if it was already (or does not exist). Flushes only."""
    result = session.execute(
        update(CaliberEventInbox)
        .where(
            CaliberEventInbox.consumer == consumer,
            CaliberEventInbox.event_id == event_id,
            CaliberEventInbox.processed_at.is_(None),
        )
        .values(processed_at=_naive(_utc(now)))
    )
    return bool(getattr(result, "rowcount", 0))


class Consumer(Protocol):
    """Something that takes events from the outbox. ``receive`` returns only once the event is durably accepted."""

    name: str

    def receive(self, envelope: dict[str, Any], payload: dict[str, Any]) -> None: ...


@dataclass
class SqlInboxConsumer:
    """The SQL consumer: its acknowledgement is a committed row in ``caliber_event_inbox``; duplicates are ignored."""

    name: str
    session_factory: Callable[[], Session]

    def receive(self, envelope: dict[str, Any], payload: dict[str, Any]) -> None:
        with self.session_factory() as session:
            receive(session, self.name, envelope, payload)
            session.commit()


# ---------------------------------------------------------------- the publisher


@dataclass(frozen=True)
class PublishResult:
    """What one :meth:`OutboxPublisher.publish_batch` did."""

    delivered: int = 0
    retried: int = 0
    dead: int = 0

    @property
    def claimed(self) -> int:
        return self.delivered + self.retried + self.dead


@dataclass
class OutboxPublisher:
    """Claims due outbox rows and hands them to the consumers, with retry, backoff and a lease for crashed claimers.

    ``clock`` is injectable so tests drive time instead of sleeping. A row a publisher claimed and then died holding
    becomes due again when its lease expires.
    """

    session_factory: Callable[[], Session]
    consumers: Sequence[Consumer] = ()
    owner_id: str = field(default_factory=lambda: f"outbox-{uuid.uuid4().hex[:12]}")
    batch_size: int = 50
    lease_seconds: float = 30.0
    poll_seconds: float = 1.0
    max_attempts: int = 20
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 300.0
    retention_days: float = 7.0
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _ticks: int = field(default=0, init=False, repr=False)

    # -- one batch -------------------------------------------------------------------------

    def publish_batch(self) -> PublishResult:
        """Claim up to ``batch_size`` due rows, hand each to every consumer, and settle each row."""
        now = _naive(self.clock())
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(CaliberEventOutbox)
                    .where(
                        CaliberEventOutbox.status == PENDING,
                        CaliberEventOutbox.next_attempt_at <= now,
                        or_(
                            CaliberEventOutbox.lease_expires_at.is_(None),
                            CaliberEventOutbox.lease_expires_at <= now,
                        ),
                    )
                    .order_by(CaliberEventOutbox.next_attempt_at, CaliberEventOutbox.created_at)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            claimed = [
                (row.event_id, dict(row.envelope), dict(row.payload), int(row.attempts))
                for row in rows
            ]
            for row in rows:
                row.claimed_by = self.owner_id
                row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            session.commit()
        delivered = retried = dead = 0
        for event_id, envelope, payload, attempts in claimed:
            error = self._hand_off(envelope, payload)
            if error is None:
                self._settle_delivered(event_id)
                delivered += 1
            elif self._settle_failed(event_id, attempts + 1, error):
                dead += 1
            else:
                retried += 1
        return PublishResult(delivered=delivered, retried=retried, dead=dead)

    def _hand_off(self, envelope: dict[str, Any], payload: dict[str, Any]) -> str | None:
        """``None`` when every consumer acknowledged; otherwise why not (nothing of the payload)."""
        for consumer in self.consumers:
            try:
                consumer.receive(envelope, payload)
            except Exception as exc:
                logger.warning(
                    "outbox handoff of %s to %s failed",
                    envelope.get("id"),
                    consumer.name,
                    exc_info=True,
                )
                return f"{consumer.name}: {type(exc).__name__}: {exc}"[:_MAX_ERROR]
        return None

    def _settle_delivered(self, event_id: str) -> None:
        with self.session_factory() as session:
            session.execute(
                update(CaliberEventOutbox)
                .where(
                    CaliberEventOutbox.event_id == event_id, CaliberEventOutbox.status == PENDING
                )
                .values(
                    status=DELIVERED,
                    delivered_at=_naive(self.clock()),
                    claimed_by=None,
                    lease_expires_at=None,
                    last_error=None,
                )
            )
            session.commit()

    def _settle_failed(self, event_id: str, attempts: int, error: str) -> bool:
        """Record a failed attempt; ``True`` when the row is now dead."""
        now = _naive(self.clock())
        dead = attempts >= self.max_attempts
        delay = min(
            self.backoff_cap_seconds, self.backoff_base_seconds * (2 ** max(0, attempts - 1))
        )
        with self.session_factory() as session:
            session.execute(
                update(CaliberEventOutbox)
                .where(
                    CaliberEventOutbox.event_id == event_id, CaliberEventOutbox.status == PENDING
                )
                .values(
                    status=DEAD if dead else PENDING,
                    attempts=attempts,
                    next_attempt_at=now + timedelta(seconds=delay),
                    claimed_by=None,
                    lease_expires_at=None,
                    last_error=error,
                )
            )
            session.commit()
        return dead

    # -- housekeeping ----------------------------------------------------------------------

    def prune_delivered(self) -> int:
        """Delete delivered rows older than the retention window; returns how many."""
        cutoff = _naive(self.clock()) - timedelta(days=self.retention_days)
        with self.session_factory() as session:
            result = session.execute(
                delete(CaliberEventOutbox).where(
                    CaliberEventOutbox.status == DELIVERED, CaliberEventOutbox.delivered_at < cutoff
                )
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    # -- the owner-only loop ---------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("OutboxPublisher.start() called while already running")
        self._task = asyncio.create_task(self._run(), name="caliber.event_outbox")

    async def stop(self, *, grace_seconds: float = 5.0) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), grace_seconds)

    async def _run(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self.publish_batch)
                self._ticks += 1
                if self._ticks % 60 == 0:
                    await asyncio.to_thread(self.prune_delivered)
            except Exception:  # the loop must outlive a database blip; the next tick retries
                logger.exception("outbox publish tick failed")
                result = PublishResult()
            if result.claimed == 0:
                await asyncio.sleep(self.poll_seconds)


def outbox_depth(session: Session) -> dict[str, Any]:
    """``{"pending", "delivered", "dead", "oldest_pending"}``: what an operator watches (the oldest pending row's
    creation time is the lag of the slowest undelivered event)."""
    counts: dict[str, int] = {
        str(status): int(total)
        for status, total in session.execute(
            select(CaliberEventOutbox.status, func.count()).group_by(CaliberEventOutbox.status)
        ).all()
    }
    oldest = session.execute(
        select(func.min(CaliberEventOutbox.created_at)).where(CaliberEventOutbox.status == PENDING)
    ).scalar_one()
    return {
        "pending": int(counts.get(PENDING, 0)),
        "delivered": int(counts.get(DELIVERED, 0)),
        "dead": int(counts.get(DEAD, 0)),
        "oldest_pending": oldest,
    }


__all__ = [
    "DEAD",
    "DELIVERED",
    "PENDING",
    "Consumer",
    "OutboxConflict",
    "OutboxPublisher",
    "PublishResult",
    "SqlInboxConsumer",
    "bind_outbox",
    "build_envelope",
    "claim_unprocessed",
    "enqueue",
    "mark_processed",
    "outbox_active",
    "outbox_depth",
    "outbox_enabled",
    "payload_hash",
    "receive",
]
