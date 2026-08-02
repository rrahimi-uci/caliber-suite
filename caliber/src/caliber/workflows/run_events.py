"""Contiguous, conflict-free sequence allocation for workflow run events.

Three writers append run events — the launch path
(:mod:`caliber.workflows.run_launch`), the route layer
(:mod:`caliber.routes.workflow_runs`), and the run worker
(:mod:`caliber.orchestrator.workflow_run_worker`). Each did the same thing
independently::

    SELECT max(sequence) WHERE workflow_run_id = :run   -- no lock
    INSERT ... sequence = max + 1

Two writers interleaving between the read and the insert compute the same
``max + 1``. ``uq_workflow_run_event_sequence`` then rejects the loser, so the
outcome is not a duplicated event but a *lost* one: whichever caller does not
handle ``IntegrityError`` drops the event, or fails the request or run that was
trying to record it. A cancel arriving while the worker is writing progress is
exactly this shape.

Two mechanisms, because neither is sufficient alone:

**A row lock on the parent run.** ``SELECT ... FOR UPDATE`` on the run row
serializes allocation for that run without touching other runs. SQLAlchemy omits
``FOR UPDATE`` on SQLite, which has no row locking — so this is the real fix on
PostgreSQL and a no-op in the test database.

**Bounded retry on the constraint.** This is what covers SQLite, a lock that was
not taken because the run row is missing, and any writer added later that forgets
to lock. The constraint is the authority; the lock is the optimization that keeps
retries rare.

Allocation is deliberately *not* a database sequence or an autoincrement column.
The value is a per-run ordinal that consumers read as a dense, gap-free ordering
of what happened in that run, and a shared sequence would leave holes wherever a
transaction rolled back.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkflowRun, CaliberWorkflowRunEvent

logger = logging.getLogger("caliber.workflows.run_events")

#: Attempts before giving up. Contention is between a handful of writers on one
#: run, so a conflict that survives this many re-reads is a real fault rather
#: than a race worth waiting out.
_MAX_ATTEMPTS: Final[int] = 5


def _lock_run(session: Session, workflow_run_id: str) -> None:
    """Take a row lock on the parent run, where the backend supports one.

    Locking the *run* rather than the event table keeps concurrent runs
    independent — the contention that matters is between writers on one run.
    A missing run row is not an error here: events are occasionally recorded for
    a run being created in the same transaction, and the retry below is what
    makes that safe.
    """
    session.execute(
        select(CaliberWorkflowRun.workflow_run_id)
        .where(CaliberWorkflowRun.workflow_run_id == workflow_run_id)
        .with_for_update()
    ).first()


def _current_max(session: Session, workflow_run_id: str) -> int:
    """Highest sequence recorded for this run, or 0.

    A named seam rather than an inline subquery so a test can return a *stale*
    value and drive the conflict path deterministically. Reproducing that race
    with real threads requires row-level locking the SQLite test database does
    not have, and a threaded approximation of it would be a flake generator.
    """
    current = (
        session.execute(
            select(func.max(CaliberWorkflowRunEvent.sequence)).where(
                CaliberWorkflowRunEvent.workflow_run_id == workflow_run_id
            )
        )
        .scalars()
        .first()
    )
    return int(current or 0)


def append_run_event(
    session: Session,
    *,
    workflow_run_id: str,
    project_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    node_id: str | None = None,
) -> CaliberWorkflowRunEvent:
    """Append one run event, allocating the next contiguous sequence number.

    Flushes so the sequence conflict — if any — surfaces here where it can be
    retried, rather than at the caller's commit where the whole transaction is
    already lost.
    """
    last_error: IntegrityError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        savepoint = session.begin_nested()
        try:
            _lock_run(session, workflow_run_id)
            current = _current_max(session, workflow_run_id)
            event = CaliberWorkflowRunEvent(
                workflow_run_id=workflow_run_id,
                project_id=project_id,
                sequence=current + 1,
                event_type=event_type,
                node_id=node_id,
                payload=payload,
            )
            session.add(event)
            session.flush()
        except IntegrityError as exc:
            # Roll back only this allocation, not the caller's work. Without the
            # savepoint a retry would run inside a session the failed flush had
            # already invalidated.
            savepoint.rollback()
            last_error = exc
            logger.debug(
                "run event sequence conflict for %s (attempt %d/%d), retrying",
                workflow_run_id,
                attempt + 1,
                _MAX_ATTEMPTS,
            )
            continue
        savepoint.commit()
        return event

    assert last_error is not None
    raise last_error
