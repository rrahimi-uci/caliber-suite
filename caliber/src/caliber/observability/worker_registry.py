"""Worker self-registration, so an *idle* worker's liveness is observable.

:mod:`caliber.observability.queue_health` derived ``workers_alive`` from claimed
``running`` rows. That works only while there is work: with an empty queue there is
nothing to infer from, so a dead idle worker and a healthy idle worker produced the
same reading, and the encoded empty-queue contract called it healthy. The outage
became visible only after a backlog had accumulated.

This module inverts the direction of evidence. Each worker writes its own row every
poll cycle, so:

* a **fresh** row is direct evidence the loop is turning, with no queued work needed;
* a **stale** row is evidence the worker stopped, and names which one; and
* **no** row at all is evidence nothing ever registered — a deployment that never
  started its worker, which the inferred signal could not distinguish from idleness.

Staleness is judged against the worker's own poll interval rather than a second
configured threshold, so the verdict cannot drift from the cadence it is measuring
(see :func:`stale_after_seconds`).

A heartbeat write must never break a tick: :func:`record_heartbeat` swallows and logs
failures. Losing an observability write is an operational nuisance; failing a run
because the observability table is unavailable is an outage this module would have
caused rather than reported.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from caliber.db.models import CaliberWorkerHeartbeat

logger = logging.getLogger("caliber.observability.worker_registry")

#: Worker kinds. Each is its own queue with its own loop, so they are reported
#: separately — one kind being down must not read as all of them being down.
KIND_WORKFLOW_RUN = "workflow_run"

#: Multiple of the poll interval before a heartbeat is considered stale, plus a floor.
#: A worker executing a long run heartbeats from its lease loop, not its poll loop, so
#: the tolerance has to exceed one cycle by a comfortable margin or a busy worker would
#: be reported dead. Three cycles means two consecutive misses are tolerated.
_STALE_INTERVAL_MULTIPLE = 3.0
_STALE_FLOOR_SECONDS = 30.0


def stale_after_seconds(interval_seconds: float, lease_seconds: float = 0.0) -> float:
    """How old a heartbeat may be before its worker is presumed dead.

    Derived from the worker's own poll interval and lease duration rather than
    configured separately: a standalone threshold silently becomes wrong the moment
    an operator retunes the cadence, and would then report either false outages or
    none at all.
    """
    from_interval = max(float(interval_seconds), 0.0) * _STALE_INTERVAL_MULTIPLE
    # A worker mid-run reports through its lease loop, so the lease window is a floor
    # too — otherwise a long-running node would look like a stalled worker.
    return max(from_interval, float(lease_seconds), _STALE_FLOOR_SECONDS)


@dataclass(frozen=True)
class WorkerRecord:
    """One registered worker, as reported to operators."""

    worker_id: str
    kind: str
    hostname: str | None
    ticks: int
    heartbeat_age_seconds: float
    alive: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "kind": self.kind,
            "hostname": self.hostname,
            "ticks": self.ticks,
            "heartbeat_age_seconds": round(self.heartbeat_age_seconds, 3),
            "alive": self.alive,
        }


def _aware(value: datetime | None) -> datetime | None:
    """Normalize a DB-roundtripped timestamp to UTC-aware (SQLite drops tzinfo)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def record_heartbeat(
    session: Any,
    *,
    worker_id: str,
    kind: str = KIND_WORKFLOW_RUN,
    now: datetime | None = None,
) -> None:
    """Upsert this worker's liveness row and advance its tick count.

    Called from the poll loop on **every** cycle including idle ones — that is the
    entire point. Never raises: see the module docstring.
    """
    stamp = now or datetime.now(timezone.utc)
    try:
        row = session.get(CaliberWorkerHeartbeat, worker_id)
        if row is None:
            session.add(
                CaliberWorkerHeartbeat(
                    worker_id=worker_id,
                    kind=kind,
                    hostname=_hostname(),
                    started_at=stamp,
                    last_heartbeat_at=stamp,
                    ticks=1,
                )
            )
        else:
            row.last_heartbeat_at = stamp
            row.kind = kind
            row.ticks = int(row.ticks or 0) + 1
        session.commit()
    except Exception:
        logger.warning("could not record worker heartbeat for %s", worker_id, exc_info=True)
        # Roll back so the caller's session is reusable for the actual work; a
        # poisoned session would turn a failed observability write into a failed tick.
        try:
            session.rollback()
        except Exception:  # pragma: no cover - nothing further can be done
            logger.debug("rollback after heartbeat failure also failed", exc_info=True)


def deregister(session: Any, *, worker_id: str) -> None:
    """Remove this worker's row on a clean shutdown.

    A graceful stop is not an outage, so leaving the row to go stale would report a
    planned deploy as a failure. An *unclean* exit deliberately leaves the row behind
    — that stale row is the signal.
    """
    try:
        session.execute(
            delete(CaliberWorkerHeartbeat).where(CaliberWorkerHeartbeat.worker_id == worker_id)
        )
        session.commit()
    except Exception:
        logger.warning("could not deregister worker %s", worker_id, exc_info=True)
        try:
            session.rollback()
        except Exception:  # pragma: no cover
            logger.debug("rollback after deregister failure also failed", exc_info=True)


def list_workers(
    session: Any,
    *,
    kind: str = KIND_WORKFLOW_RUN,
    stale_after: float,
    now: datetime | None = None,
) -> list[WorkerRecord]:
    """Every registered worker of ``kind``, alive or stale, newest heartbeat first.

    Stale workers are **returned, not filtered out**: "a worker registered and then
    stopped reporting" is the most actionable operational fact here, and dropping
    those rows would reduce it to the same silence this module exists to remove.
    """
    stamp = now or datetime.now(timezone.utc)
    window = timedelta(seconds=max(float(stale_after), 0.0))
    rows = (
        session.execute(
            select(CaliberWorkerHeartbeat)
            .where(CaliberWorkerHeartbeat.kind == kind)
            .order_by(CaliberWorkerHeartbeat.last_heartbeat_at.desc())
        )
        .scalars()
        .all()
    )
    records: list[WorkerRecord] = []
    for row in rows:
        beat = _aware(row.last_heartbeat_at) or stamp
        age = (stamp - beat).total_seconds()
        records.append(
            WorkerRecord(
                worker_id=row.worker_id,
                kind=row.kind,
                hostname=row.hostname,
                ticks=int(row.ticks or 0),
                heartbeat_age_seconds=age,
                alive=age <= window.total_seconds(),
            )
        )
    return records


def _hostname() -> str | None:
    try:
        return os.uname().nodename
    except Exception:  # pragma: no cover - non-POSIX
        return None


__all__ = [
    "KIND_WORKFLOW_RUN",
    "WorkerRecord",
    "deregister",
    "list_workers",
    "record_heartbeat",
    "stale_after_seconds",
]
