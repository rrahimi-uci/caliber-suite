"""The single-owner execution lease and its fencing token (B13.2, #668; ADR 0002).

In the reference profile exactly one process holds the *execution-owner lease* at a time and only the holder runs
the background loops; every other replica serves requests only. This module is the lease, and nothing else: wiring
it into the lifespan is #673 and the outbox is #685.

A :class:`Lease` is what a process captured when it acquired: ``(name, owner_id, fencing_token, expires_at)``.

* **Database time.** Expiry is decided by the database's clock inside the statement that tests it. A process's own
  clock never extends or shortens a lease, so a paused or skewed process cannot keep one it has lost.
* **Compare-and-set on the exact owner and token.** Renewal, release and every fenced transition require *both* the
  owner id and the token the caller captured, and an unexpired lease. They never compare ``token < latest`` or read a
  fresh "current token" to compare against: a stale owner holds a token that is simply no longer the row's.
* **Renewal keeps the token; a new acquisition raises it.** :func:`renew` (and only it) leaves ``fencing_token``
  alone and requires the exact lease the caller captured. :func:`acquire` never renews: while a lease is held and
  unexpired it returns ``None`` *even to the same ``owner_id``*, so a restarted process that reuses a stable id cannot
  adopt the token of a predecessor that may still be alive. Acquiring after the lease expired, from anyone including
  the previous holder, adds one, so anything a lapsed holder started is distinguishable from the new holder's work.
* **Fencing.** :func:`assert_fenced` runs in the *caller's* transaction, checks the exact lease and an unexpired
  expiry, and on PostgreSQL holds a row lock until the transaction ends. A takeover must update that row, so it
  waits for the fenced transition to finish: the work is either done under a valid lease or refused with
  :class:`LeaseLost`, never done after losing it (the check happens under a valid lease and the transition is
  serialized before any takeover; the commit itself may land after the database-time expiry). **SQLite gives no such
  guarantee** (the driver begins no transaction before the first write, so the check holds nothing); it is for
  development and the reference profile refuses it.
* **Isolation.** The statements are written for READ COMMITTED (PostgreSQL's default): a contended :func:`acquire`
  blocks and then returns ``None``. Under REPEATABLE READ or SERIALIZABLE a contended :func:`acquire`, or a fence that
  races a heartbeat committed after the snapshot, raises :class:`LeaseLost` (a serialization failure): it fails
  closed, but "returns ``None``" is a READ COMMITTED promise.
* **Liveness (for the loop wiring, #673).** ``assert_fenced`` serializes every fenced transition on one row, and a
  renewal waits for a fenced transaction in flight, so keep fenced transactions much shorter than ``ttl`` minus the
  renewal interval, renew every ``ttl / 3`` at most, and give fenced and acquiring transactions a
  ``SET LOCAL lock_timeout`` so a stuck session cannot block failover indefinitely
  (``idle_in_transaction_session_timeout`` bounds it too). Log the ``__cause__`` of a :class:`LeaseLost`: every
  database error is reported as "the lease store is unavailable", programming errors included.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import DateTime, literal_column, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from caliber.db.models import CaliberExecutionOwner

DEFAULT_LEASE: Final = "execution-owner"
#: The longest lease a caller may ask for; a longer one is a mistake (a stuck process would hold it for hours).
MAX_TTL_SECONDS: Final = 3600.0
#: The shortest lease: below a second the database clock's resolution and a heartbeat's own latency decide it.
MIN_TTL_SECONDS: Final = 1.0
MAX_OWNER_ID: Final = 128
MAX_LEASE_NAME: Final = 64


class LeaseError(ValueError):
    """A lease request that is malformed (a blank owner, a non-positive or absurd ttl)."""


class LeaseLost(RuntimeError):  # noqa: N818 - named for the condition, like ``GrantDenied``
    """The caller no longer holds this lease: another owner has it, it expired, or its token is not the row's."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Lease:
    """What an owner captured when it acquired."""

    name: str
    owner_id: str
    fencing_token: int
    expires_at: datetime


# ------------------------------------------------------------------ database time


def _now(session: Session) -> ColumnElement[datetime]:
    """The database's current UTC time, as a SQL expression (naive, like the column)."""
    if session.get_bind().dialect.name == "postgresql":
        return literal_column("timezone('utc', clock_timestamp())", DateTime())
    return literal_column("strftime('%Y-%m-%d %H:%M:%f', 'now')", DateTime())


def _later(session: Session, seconds: float) -> ColumnElement[datetime]:
    """The database's current UTC time plus ``seconds``."""
    if session.get_bind().dialect.name == "postgresql":
        return literal_column(
            f"timezone('utc', clock_timestamp()) + interval '{float(seconds):.3f} seconds'",
            DateTime(),
        )
    return literal_column(
        f"strftime('%Y-%m-%d %H:%M:%f', 'now', '+{float(seconds):.3f} seconds')", DateTime()
    )


def database_now(session: Session) -> datetime:
    """The database's current UTC time (naive); for tests and diagnostics, never for deciding expiry."""
    value = session.execute(select(_now(session))).scalar_one()
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _check(owner_id: str, ttl_seconds: float, name: str) -> None:
    if (
        type(owner_id) is not str
        or not owner_id.strip()
        or owner_id != owner_id.strip()
        or len(owner_id) > MAX_OWNER_ID
    ):
        raise LeaseError(
            "owner_id must be a non-empty string of at most 128 characters, with no surrounding whitespace"
        )
    if (
        type(name) is not str
        or not name.strip()
        or name != name.strip()
        or len(name) > MAX_LEASE_NAME
    ):
        raise LeaseError(
            "the lease name must be a non-empty string of at most 64 characters, unpadded"
        )
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int | float)
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
    ):
        raise LeaseError(
            f"ttl_seconds must be a number from {MIN_TTL_SECONDS:g} to {MAX_TTL_SECONDS:g}"
        )


# ------------------------------------------------------------------ acquire, renew, release


def acquire(
    session: Session, owner_id: str, *, ttl_seconds: float, name: str = DEFAULT_LEASE
) -> Lease | None:
    """Try to become (or stay) the owner. Returns the lease, or ``None`` when another owner holds an unexpired one.

    * Anyone calling when the lease has expired (or has never existed) makes a *new acquisition*: the token is
      increased by one (a first-ever acquisition is token 1).
    * While a lease is held and unexpired the answer is ``None``, for every caller including the current holder's
      own ``owner_id``: the holder renews with :func:`renew`.

    Each branch is one atomic statement, so concurrent callers resolve at the row: exactly one wins an expired
    lease (under READ COMMITTED; see the module docstring for stricter isolation levels). Flushes and does not
    commit; nobody else sees the lease until the caller's transaction commits.
    """
    _check(owner_id, ttl_seconds, name)
    now, until = _now(session), _later(session, ttl_seconds)
    try:
        taken = session.execute(
            update(CaliberExecutionOwner)
            .where(
                CaliberExecutionOwner.lease_name == name, CaliberExecutionOwner.expires_at <= now
            )
            .values(
                owner_id=owner_id,
                fencing_token=CaliberExecutionOwner.fencing_token + 1,
                expires_at=until,
                acquired_at=now,
                renewed_at=now,
            )
            .returning(CaliberExecutionOwner.fencing_token, CaliberExecutionOwner.expires_at)
            .execution_options(synchronize_session=False)
        ).first()
        if taken is not None:
            return Lease(name, owner_id, int(taken[0]), _as_datetime(taken[1]))

        inserted = session.execute(_insert_if_absent(session, name, owner_id, now, until)).first()
        if inserted is not None:
            return Lease(name, owner_id, int(inserted[0]), _as_datetime(inserted[1]))
        return None  # the row exists and another owner holds it, unexpired
    except SQLAlchemyError as exc:
        raise LeaseLost("the lease store is unavailable") from exc


def _insert_if_absent(session: Session, name: str, owner_id: str, now: Any, until: Any) -> Any:
    """``INSERT ... ON CONFLICT DO NOTHING RETURNING``: the first acquisition of a lease name, token 1."""
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

        statement: Any = pg_insert(CaliberExecutionOwner)
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: PLC0415

        statement = sqlite_insert(CaliberExecutionOwner)
    return (
        statement.values(
            lease_name=name,
            owner_id=owner_id,
            fencing_token=1,
            expires_at=until,
            acquired_at=now,
            renewed_at=now,
        )
        .on_conflict_do_nothing(index_elements=["lease_name"])
        .returning(CaliberExecutionOwner.fencing_token, CaliberExecutionOwner.expires_at)
    )


def _as_datetime(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def renew(session: Session, lease: Lease, *, ttl_seconds: float) -> Lease:
    """Extend ``lease`` and keep its token, or raise :class:`LeaseLost`.

    The statement requires the exact owner, the exact token and an unexpired lease *by database time*: a heartbeat
    that arrives after expiry, or after another owner acquired, extends nothing and never resurrects the lease.
    """
    _check(lease.owner_id, ttl_seconds, lease.name)
    now = _now(session)
    try:
        row = session.execute(
            update(CaliberExecutionOwner)
            .where(*_same(lease), CaliberExecutionOwner.expires_at > now)
            .values(expires_at=_later(session, ttl_seconds), renewed_at=now)
            .returning(CaliberExecutionOwner.expires_at)
            .execution_options(synchronize_session=False)
        ).first()
    except SQLAlchemyError as exc:
        raise LeaseLost("the lease store is unavailable") from exc
    if row is None:
        raise LeaseLost("the lease is no longer held by this owner")
    return Lease(lease.name, lease.owner_id, lease.fencing_token, _as_datetime(row[0]))


def release(session: Session, lease: Lease) -> bool:
    """Give the lease up now (expire it) if this owner and token still hold it. The token is kept, so the next
    acquisition, by anyone, increases it. Returns whether it was held."""
    now = _now(session)
    try:
        row = session.execute(
            update(CaliberExecutionOwner)
            .where(*_same(lease), CaliberExecutionOwner.expires_at > now)
            .values(expires_at=now, renewed_at=now)
            .returning(CaliberExecutionOwner.fencing_token)
            .execution_options(synchronize_session=False)
        ).first()
    except SQLAlchemyError as exc:
        raise LeaseLost("the lease store is unavailable") from exc
    return row is not None


def _same(lease: Lease) -> tuple[ColumnElement[bool], ...]:
    return (
        CaliberExecutionOwner.lease_name == lease.name,
        CaliberExecutionOwner.owner_id == lease.owner_id,
        CaliberExecutionOwner.fencing_token == lease.fencing_token,
    )


# ------------------------------------------------------------------ fencing


def assert_fenced(session: Session, lease: Lease) -> None:
    """Raise :class:`LeaseLost` unless ``lease`` is, right now, the exact held, unexpired lease.

    Call it inside the transaction that performs the owner-only transition, before the transition. On PostgreSQL it
    locks the lease row (``FOR UPDATE``) until that transaction ends, so a takeover waits for the transition instead
    of overtaking it. It compares the caller's captured owner and token for equality; it never asks whether the token
    is "the latest".
    """
    try:
        held = session.execute(
            select(CaliberExecutionOwner.fencing_token)
            .where(*_same(lease), CaliberExecutionOwner.expires_at > _now(session))
            .with_for_update()
        ).first()
    except SQLAlchemyError as exc:
        raise LeaseLost("the lease store is unavailable") from exc
    if held is None:
        raise LeaseLost("the lease is no longer held by this owner")


def current_lease(session: Session, name: str = DEFAULT_LEASE) -> Lease | None:
    """The row as stored (owner, token, expiry), expired or not; for diagnostics and tests, never an authority."""
    row = session.get(CaliberExecutionOwner, name, populate_existing=True)
    if row is None:
        return None
    return Lease(row.lease_name, row.owner_id, int(row.fencing_token), row.expires_at)


__all__ = [
    "DEFAULT_LEASE",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "Lease",
    "LeaseError",
    "LeaseLost",
    "acquire",
    "assert_fenced",
    "current_lease",
    "database_now",
    "release",
    "renew",
]
