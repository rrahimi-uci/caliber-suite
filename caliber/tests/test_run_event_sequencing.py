"""Run-event sequences must stay contiguous and unique when writers collide.

Three writers append run events, and each used to do an unlocked
``SELECT max(sequence)`` followed by an insert of ``max + 1``. The unique
constraint turns the interleaving into a *lost* event rather than a duplicate
one, which is harder to notice: a cancel arriving while the worker writes
progress simply does not appear.

The race is driven deterministically through ``_current_max`` rather than with
threads. Reproducing it faithfully needs row-level locking, which the SQLite
test database does not have — a threaded version failed on SQLite's file-level
write lock before reaching any CALIBER code, and tuning it until it passed would
have produced a flake generator that proved nothing about the allocator.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberProject, CaliberWorkflow, CaliberWorkflowRun
from caliber.db.models import CaliberWorkflowRunEvent as Event
from caliber.workflows import run_events
from caliber.workflows.run_events import append_run_event


@pytest.fixture
def seeded_run(db_session: Session) -> str:
    db_session.add_all(
        [
            CaliberProject(project_id="PRJ-seq", tenant_id="t", name="Seq", owner="@test"),
            CaliberWorkflow(workflow_id="wf-seq", project_id="PRJ-seq", name="Seq", owner="@test"),
            CaliberWorkflowRun(
                workflow_run_id="RUN-seq",
                workflow_id="wf-seq",
                project_id="PRJ-seq",
                status="running",
            ),
        ]
    )
    db_session.commit()
    return "RUN-seq"


def _seed_first_event(session_factory: sessionmaker[Session], run_id: str) -> None:
    with session_factory() as setup:
        append_run_event(
            setup, workflow_run_id=run_id, project_id="PRJ-seq", event_type="first"
        )
        setup.commit()


def test_a_stale_read_retries_instead_of_losing_the_event(
    session_factory: sessionmaker[Session],
    seeded_run: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact interleaving: a writer computes a sequence another already took.

    ``_current_max`` returns a stale 0 on the first call, so the allocator builds
    sequence 1 for a run that already has one. Before the shared allocator this
    raised ``IntegrityError`` out of the caller and the event was dropped; now it
    re-reads and takes the next free ordinal.
    """
    _seed_first_event(session_factory, seeded_run)

    real = run_events._current_max
    calls = {"n": 0}

    def _stale_once(session: Session, workflow_run_id: str) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0  # the value a concurrent writer would have read
        return real(session, workflow_run_id)

    monkeypatch.setattr(run_events, "_current_max", _stale_once)

    with session_factory() as session:
        event = append_run_event(
            session, workflow_run_id=seeded_run, project_id="PRJ-seq", event_type="second"
        )
        session.commit()

    assert calls["n"] >= 2, "the conflicting attempt did not retry"
    assert event.sequence == 2, f"expected the next free ordinal, got {event.sequence}"

    with session_factory() as check:
        sequences = sorted(
            check.execute(select(Event.sequence).where(Event.workflow_run_id == seeded_run))
            .scalars()
            .all()
        )
    assert sequences == [1, 2], f"sequences must stay contiguous, got {sequences}"


def test_a_conflict_that_never_clears_is_raised_not_swallowed(
    session_factory: sessionmaker[Session],
    seeded_run: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry is bounded. A permanent conflict must surface, not spin or vanish.

    Silently returning after exhausting attempts would reintroduce the original
    defect with extra steps.
    """
    _seed_first_event(session_factory, seeded_run)
    monkeypatch.setattr(run_events, "_current_max", lambda _session, _run: 0)

    with session_factory() as session, pytest.raises(IntegrityError):
        append_run_event(
            session, workflow_run_id=seeded_run, project_id="PRJ-seq", event_type="doomed"
        )


def test_a_retry_does_not_discard_the_callers_other_work(
    session_factory: sessionmaker[Session],
    seeded_run: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The savepoint exists so a retry does not roll back the caller's transaction.

    Callers append events inside a transaction that is also updating run status,
    so an allocation retry that discarded that work would trade a lost event for
    a lost state transition.
    """
    _seed_first_event(session_factory, seeded_run)

    real = run_events._current_max
    calls = {"n": 0}

    def _stale_once(session: Session, workflow_run_id: str) -> int:
        calls["n"] += 1
        return 0 if calls["n"] == 1 else real(session, workflow_run_id)

    monkeypatch.setattr(run_events, "_current_max", _stale_once)

    with session_factory() as session:
        run = session.get(CaliberWorkflowRun, seeded_run)
        assert run is not None
        run.status = "cancelled"
        session.flush()
        append_run_event(
            session, workflow_run_id=seeded_run, project_id="PRJ-seq", event_type="cancelled"
        )
        session.commit()

    with session_factory() as check:
        persisted = check.get(CaliberWorkflowRun, seeded_run)
        assert persisted is not None
        assert persisted.status == "cancelled", "the retry rolled back the caller's update"
        total = check.execute(
            select(func.count()).select_from(Event).where(Event.workflow_run_id == seeded_run)
        ).scalar_one()
    assert total == 2


def test_allocation_is_scoped_per_run(
    session_factory: sessionmaker[Session], db_session: Session, seeded_run: str
) -> None:
    """Locking the parent run must not serialize unrelated runs into one counter."""
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="RUN-other",
            workflow_id="wf-seq",
            project_id="PRJ-seq",
            status="running",
        )
    )
    db_session.commit()

    with session_factory() as session:
        for _ in range(3):
            append_run_event(
                session, workflow_run_id=seeded_run, project_id="PRJ-seq", event_type="a"
            )
        first = append_run_event(
            session, workflow_run_id="RUN-other", project_id="PRJ-seq", event_type="b"
        )
        session.commit()

    assert first.sequence == 1


def test_events_for_a_run_row_that_does_not_exist_yet_still_allocate(
    session_factory: sessionmaker[Session],
) -> None:
    """The row lock must not become a requirement that the run row already exists.

    Events are occasionally recorded for a run being created in the same
    transaction; a missing row means no lock is taken, and the constraint plus
    retry carries the guarantee.
    """
    with session_factory() as session:
        event = append_run_event(
            session,
            workflow_run_id="RUN-not-yet-persisted",
            project_id=None,
            event_type="queued",
        )
        session.commit()
    assert event.sequence == 1
