"""The consumer/source/event-id inbox: an inbound event is applied once (B13.5a, #689; ADR 0002).

A webhook or connector callback can arrive twice (the sender retries after a timeout), at the same time on two workers,
or after the process died half way through. Three things must hold together, so they are one transaction:

1. **The reservation.** ``(consumer, source, event_id)`` is the primary key of ``caliber_inbox_events``; inserting the
   row is claiming the event. On PostgreSQL a second transaction inserting the same key waits for the first to finish
   (the unique index is the lock): if the first committed it is a duplicate, if it rolled back the second proceeds.
   SQLite allows one writer at a time: a concurrent duplicate fails at once with a lock error (never applied twice) and
   the sender's retry is then a ``duplicate``.
2. **The business transition**, run by the caller's ``apply`` callback in the caller's session.
3. **The processed marker**, set on the reservation row.

:func:`process_once` does all three inside savepoints and never commits: the caller commits (or rolls back) the
transaction it owns, so the reservation, the transition and the marker stand or fall together. If ``apply`` raises, the
reservation is removed with it, and the sender's retry finds no trace and is processed: **a failed transaction is
retryable, never treated as already processed.** If the process dies before the commit, the database discards the whole
transaction and the outcome is the same.

Handing over to a durable queue before acknowledging is the same thing with a smaller ``apply``: pass a callback that
:func:`caliber.events.outbox.enqueue`\\s a work item, and the reservation and the queued item commit together; the
sender may be answered as soon as that commit returns.

Identity and content are separate. The key says *which* event; ``payload_hash`` says *what it said*. The same key with
the same hash is a ``duplicate`` (acknowledge it, apply nothing). The same key with a different hash is not a retry but a
different message reusing an id, and raises :class:`InboxPayloadMismatch` (the webhook route answers ``409``); it is
never applied and never overwrites the stored hash.

``apply`` may raise :class:`InboxIgnored` for an event that is valid but must not change state (for example one that
arrives older than the state it would change): whatever ``apply`` wrote is rolled back, the row is recorded as
``ignored`` and committed, so a redelivery is a duplicate rather than a second look.

Limits, stated plainly: this is **at-most-once application per inbox key, not exactly-once delivery** (the sender may
still send it many times; each is acknowledged). ``apply`` must not commit or roll back the session; it runs in the
caller's transaction, so a side effect outside that transaction (a call to another service) is not covered by it and
must be idempotent on its own. Rows are kept until :func:`prune` is told to forget them; forget only what is older than
the sender's longest redelivery window, because a forgotten id is applied again.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Generic, Literal, TypeVar

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, ResourceClosedError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberInboxEvent
from caliber.db.transactions import begin_sqlite_transaction

T = TypeVar("T")

RECEIVED: Final = "received"
PROCESSED: Final = "processed"
IGNORED: Final = "ignored"
DUPLICATE: Final = "duplicate"

Outcome = Literal["processed", "ignored", "duplicate"]

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TEXT = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_MAX_CONSUMER = 64
_MAX_SOURCE = 64
_MAX_EVENT_ID = 160


class InboxPayloadMismatch(ValueError):  # noqa: N818 - a refusal, named for what it says
    """The event id was already taken with a different payload: a different message reusing the id."""

    def __init__(self, consumer: str, source: str, event_id: str) -> None:
        super().__init__(
            f"inbox event {source}/{event_id!r} for {consumer!r} was already received with a different payload"
        )
        self.consumer = consumer
        self.source = source
        self.event_id = event_id


class InboxContractError(RuntimeError):
    """``apply`` committed or rolled back the session it was given, which it must not do.

    Its writes may already be committed together with a reservation still marked ``received``; such a row is a
    duplicate to every later delivery and needs an operator. The error is raised so the caller's own commit is not
    what hides it.
    """


class InboxIgnored(Exception):  # noqa: N818 - a signal from ``apply``, not an error
    """Raised by ``apply`` when the event is valid but must not change state; it is recorded as ``ignored``."""


@dataclass(frozen=True)
class InboxResult(Generic[T]):
    """What :func:`process_once` did: ``processed`` (``value`` is ``apply``'s result), ``ignored`` or ``duplicate``."""

    outcome: Outcome
    value: T | None = None

    @property
    def applied(self) -> bool:
        """Whether ``apply`` ran and committed its effect with this call."""
        return self.outcome == PROCESSED


def _utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _naive(moment: datetime) -> datetime:
    return _utc(moment).astimezone(timezone.utc).replace(tzinfo=None)


def _validate(consumer: str, source: str, event_id: str, payload_hash: str) -> None:
    for label, value, limit in (
        ("consumer", consumer, _MAX_CONSUMER),
        ("source", source, _MAX_SOURCE),
        ("event_id", event_id, _MAX_EVENT_ID),
    ):
        # Ids arrive from the outside: reject what the database cannot store (NUL, unpaired surrogates) here, as a
        # ValueError the caller can answer with a 4xx, rather than as a driver error half way through the reservation.
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
            or not _TEXT.fullmatch(value)
        ):
            raise ValueError(
                f"inbox {label} must be 1..{limit} printable characters, not blank, with no control characters"
            )
    if not isinstance(payload_hash, str) or not _HASH.fullmatch(payload_hash):
        raise ValueError("inbox payload_hash must be 'sha256:' and 64 lowercase hex digits")


def process_once(
    session: Session,
    *,
    consumer: str,
    source: str,
    event_id: str,
    payload_hash: str,
    apply: Callable[[], T],
    now: datetime | None = None,
) -> InboxResult[T]:
    """Reserve the event, run ``apply``, and mark it processed, all in the caller's transaction (flushes, never commits).

    Returns ``processed`` with ``apply``'s value, ``ignored`` if ``apply`` raised :class:`InboxIgnored`, or
    ``duplicate`` (``apply`` not called) if this identity was already taken with the same payload. Raises
    :class:`InboxPayloadMismatch` for the same identity with a different payload, ``ValueError`` for a malformed key or
    hash, and whatever ``apply`` raises (after removing the reservation, so a retry is processed).
    """
    _validate(consumer, source, event_id, payload_hash)
    key = (consumer, source, event_id)
    begin_sqlite_transaction(session)
    existing = session.get(CaliberInboxEvent, key)
    if existing is not None:
        return _already_taken(existing, payload_hash)
    reservation = session.begin_nested()
    row = CaliberInboxEvent(
        consumer=consumer,
        source=source,
        event_id=event_id,
        payload_hash=payload_hash,
        status=RECEIVED,
        received_at=_naive(_utc(now)),
    )
    try:
        session.add(row)
        session.flush()
    except IntegrityError:
        reservation.rollback()
        # Only the primary key means "another transaction took it first". Any other constraint failure is a defect the
        # caller must see: reporting it as a duplicate would acknowledge an event nobody applied.
        winner = session.get(CaliberInboxEvent, key, populate_existing=True)
        if winner is None:
            raise
        return _already_taken(winner, payload_hash)
    try:
        try:
            with session.begin_nested():
                value = apply()
        except InboxIgnored:
            row.status = IGNORED
            result: InboxResult[T] = InboxResult(IGNORED)
        else:
            row.status = PROCESSED
            result = InboxResult(PROCESSED, value)
        row.processed_at = _naive(_utc(now))
        session.flush()
        reservation.commit()
    except ResourceClosedError as exc:
        raise InboxContractError(
            "apply committed or rolled back the session; it must leave the transaction to the caller"
        ) from exc
    except BaseException:
        if reservation.is_active:
            reservation.rollback()
        raise
    return result


def _already_taken(existing: CaliberInboxEvent, payload_hash: str) -> InboxResult[Any]:
    if existing.payload_hash != payload_hash:
        raise InboxPayloadMismatch(existing.consumer, existing.source, existing.event_id)
    return InboxResult(DUPLICATE)


def prune(session: Session, *, older_than: datetime) -> int:
    """Forget the events received before ``older_than`` (flushes only); returns how many rows went.

    Forgetting an id means a redelivery of it is applied again, so ``older_than`` must be older than the sender's
    longest redelivery window.
    """
    result = session.execute(
        delete(CaliberInboxEvent)
        .where(CaliberInboxEvent.received_at < _naive(older_than))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0))
