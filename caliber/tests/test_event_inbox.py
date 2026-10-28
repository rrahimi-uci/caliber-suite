"""The consumer/source/event-id inbox (B13.5a, #689).

Every case runs on SQLite and, where ``CALIBER_TEST_POSTGRES_URL`` is set, on PostgreSQL, against a database migrated by
Alembic. The concurrency cases are PostgreSQL's: SQLite serialises writers, so a race cannot be arranged there.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberEventOutbox, CaliberInboxEvent, CaliberProject
from caliber.events.inbox import (
    DUPLICATE,
    IGNORED,
    PROCESSED,
    InboxContractError,
    InboxIgnored,
    InboxPayloadMismatch,
    process_once,
    prune,
)
from caliber.events.outbox import enqueue, payload_hash
from tests.dialect_helpers import DIALECTS
from tests.test_event_outbox import _count, _database

T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
HASH_A = payload_hash({"state": "a"})
HASH_B = payload_hash({"state": "b"})


def _take(
    session: Session,
    event_id: str = "evt-1",
    *,
    consumer: str = "billing",
    source: str = "github",
    digest: str = HASH_A,
    apply: Any = None,
) -> Any:
    calls: list[int] = []

    def business() -> str:
        calls.append(1)
        project_id = f"PRJ-{uuid.uuid4().hex[:8]}"
        session.add(CaliberProject(project_id=project_id, name=project_id, owner="@t"))
        return "applied"

    result = process_once(
        session,
        consumer=consumer,
        source=source,
        event_id=event_id,
        payload_hash=digest,
        apply=apply or business,
        now=T0,
    )
    return SimpleNamespace(
        outcome=result.outcome, value=result.value, applied=result.applied, calls=len(calls)
    )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_event_is_applied_once_and_its_redelivery_is_a_duplicate(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            first = _take(session)
            session.commit()
        assert (first.outcome, first.value, first.applied, first.calls) == (
            PROCESSED,
            "applied",
            True,
            1,
        )
        with factory() as session:
            again = _take(session)
            session.commit()
        assert (again.outcome, again.value, again.applied, again.calls) == (
            DUPLICATE,
            None,
            False,
            0,
        )
        assert _count(factory, CaliberProject) == 1, "the transition was applied once"
        with factory() as session:
            row = session.execute(select(CaliberInboxEvent)).scalar_one()
            assert (row.consumer, row.source, row.event_id) == ("billing", "github", "evt-1")
            assert (row.status, row.payload_hash) == (PROCESSED, HASH_A)
            assert row.processed_at == T0.replace(tzinfo=None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_same_id_with_different_content_is_refused_and_changes_nothing(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            _take(session)
            session.commit()
        with factory() as session:
            with pytest.raises(InboxPayloadMismatch) as refused:
                _take(session, digest=HASH_B)
            session.rollback()
        assert (refused.value.consumer, refused.value.source, refused.value.event_id) == (
            "billing",
            "github",
            "evt-1",
        )
        assert "evt-1" in str(refused.value) and HASH_B not in str(refused.value)
        assert _count(factory, CaliberProject) == 1, "the different message was not applied"
        with factory() as session:
            assert session.execute(select(CaliberInboxEvent.payload_hash)).scalar_one() == HASH_A


@pytest.mark.parametrize("dialect", DIALECTS)
def test_consumer_source_and_event_id_each_make_a_separate_identity(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            outcomes = [
                _take(session).outcome,
                _take(session, consumer="audit").outcome,
                _take(session, source="connector").outcome,
                _take(session, event_id="evt-2").outcome,
                _take(session).outcome,
            ]
            session.commit()
        assert outcomes == [PROCESSED] * 4 + [DUPLICATE]
        assert _count(factory, CaliberInboxEvent) == 4
        assert _count(factory, CaliberProject) == 4


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_failing_transition_leaves_no_reservation_so_the_retry_is_processed(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            session.add(CaliberProject(project_id="PRJ-before", name="PRJ-before", owner="@t"))

            def boom() -> None:
                session.add(CaliberProject(project_id="PRJ-half", name="PRJ-half", owner="@t"))
                session.flush()
                raise RuntimeError("the transition failed half way")

            with pytest.raises(RuntimeError, match="half way"):
                _take(session, apply=boom)
            assert (
                session.execute(select(func.count()).select_from(CaliberInboxEvent)).scalar_one()
                == 0
            ), "the reservation went with the failed transition, even before the caller rolls back"
            retried = _take(session)
            session.commit()
        assert retried.outcome == PROCESSED
        with factory() as session:
            ids = set(session.execute(select(CaliberProject.project_id)).scalars())
        assert "PRJ-before" in ids and "PRJ-half" not in ids, (
            "only the failed transition was undone"
        )
        assert _count(factory, CaliberInboxEvent) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_transition_that_hits_a_database_constraint_is_retryable(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            session.add(CaliberProject(project_id="PRJ-taken", name="PRJ-taken", owner="@t"))
            session.commit()

        with factory() as session:

            def collide() -> None:
                session.add(CaliberProject(project_id="PRJ-again", name="PRJ-taken", owner="@t"))
                session.flush()

            with pytest.raises(IntegrityError):
                _take(session, apply=collide)
            assert _take(session).outcome == PROCESSED, (
                "the session is usable and the event is not marked"
            )
            session.commit()
        assert _count(factory, CaliberInboxEvent) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_an_interrupt_in_the_transition_also_leaves_no_reservation(
    dialect: str, escape: type[BaseException], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory), factory() as session:

        def interrupted() -> None:
            raise escape

        with pytest.raises(escape):
            _take(session, apply=interrupted)
        assert _take(session).outcome == PROCESSED, "the session is usable and nothing was recorded"
        session.commit()
        assert _count(factory, CaliberInboxEvent) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_a_transition_that_ends_the_transaction_itself_is_refused_loudly(
    dialect: str, finish: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory), factory() as session:

        def ends_the_transaction() -> None:
            session.add(CaliberProject(project_id="PRJ-own", name="PRJ-own", owner="@t"))
            getattr(session, finish)()

        with pytest.raises(InboxContractError, match="leave the transaction to the caller"):
            _take(session, apply=ends_the_transaction)
        session.rollback()
        # What apply committed stays committed; the reservation, if it was committed with it, is still ``received``.
        stuck = _count(factory, CaliberInboxEvent, CaliberInboxEvent.status == "received")
        assert stuck == (1 if finish == "commit" else 0)
        assert _count(factory, CaliberInboxEvent, CaliberInboxEvent.status == PROCESSED) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_caller_commits_or_rolls_back_the_reservation_the_transition_and_the_marker_together(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            assert _take(session).outcome == PROCESSED
            session.rollback()  # the process dies, or the request fails, after the call and before the commit
        assert (_count(factory, CaliberInboxEvent), _count(factory, CaliberProject)) == (0, 0)
        with factory() as session:
            assert _take(session).outcome == PROCESSED, (
                "nothing was recorded, so the retry is processed"
            )
            session.commit()
        assert (_count(factory, CaliberInboxEvent), _count(factory, CaliberProject)) == (1, 1)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_event_that_must_not_change_state_is_recorded_ignored_and_what_apply_wrote_is_undone(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):

        def stale() -> None:
            session.add(CaliberProject(project_id="PRJ-stale", name="PRJ-stale", owner="@t"))
            session.flush()
            raise InboxIgnored

        with factory() as session:
            result = _take(session, apply=stale)
            session.commit()
        assert (result.outcome, result.value, result.applied) == (IGNORED, None, False)
        assert _count(factory, CaliberProject) == 0
        with factory() as session:
            row = session.execute(select(CaliberInboxEvent)).scalar_one()
            assert (row.status, row.processed_at) == (IGNORED, T0.replace(tzinfo=None))
            assert _take(session).outcome == DUPLICATE, "an ignored event is not looked at twice"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_handing_the_event_to_a_durable_work_item_commits_with_the_reservation(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):

        def queue(session: Session) -> str:
            enqueue(
                session,
                event_id="work-evt-1",
                event_type="caliber.inbound",
                aggregate_type="inbound",
                aggregate_id="evt-1",
                project_id=None,
                payload={"from": "github"},
                now=T0,
            )
            return "queued"

        with factory() as session:
            assert _take(session, apply=lambda: queue(session)).value == "queued"
            session.rollback()
        assert (_count(factory, CaliberInboxEvent), _count(factory, CaliberEventOutbox)) == (0, 0)
        with factory() as session:
            assert _take(session, apply=lambda: queue(session)).outcome == PROCESSED
            assert _take(session, apply=lambda: queue(session)).outcome == DUPLICATE
            session.commit()
        assert (_count(factory, CaliberInboxEvent), _count(factory, CaliberEventOutbox)) == (1, 1)


@pytest.mark.parametrize(
    ("consumer", "source", "event_id", "digest", "needle"),
    [
        ("", "github", "e", HASH_A, "consumer"),
        ("   ", "github", "e", HASH_A, "consumer"),
        ("c" * 65, "github", "e", HASH_A, "consumer"),
        ("billing", "", "e", HASH_A, "source"),
        ("billing", "s" * 65, "e", HASH_A, "source"),
        ("billing", "github", "", "sha256:" + "a" * 64, "event_id"),
        ("billing", "github", "e" * 161, HASH_A, "event_id"),
        ("billing", "github", "e", "", "payload_hash"),
        ("billing", "github", "e", "sha256:" + "A" * 64, "payload_hash"),
        ("billing", "github", "e", "sha256:abc", "payload_hash"),
        ("billing", "github", "e", "md5:" + "a" * 64, "payload_hash"),
        ("billing", "github", "a\x00b", HASH_A, "event_id"),
        ("billing", "github", "a\nb", HASH_A, "event_id"),
        ("billing", "gi\x7fthub", "e", HASH_A, "source"),
        ("bill\x85ing", "github", "e", HASH_A, "consumer"),
        ("billing", "github", "a<lone-surrogate>b", HASH_A, "event_id"),
        ("billing", b"github", "e", HASH_A, "source"),
        ("billing", "github", 5, HASH_A, "event_id"),
        ("billing", "github", "e", None, "payload_hash"),
    ],
)
def test_a_malformed_identity_or_hash_is_refused_before_the_database_is_touched(
    consumer: str, source: str, event_id: str, digest: str, needle: str
) -> None:
    class Untouched:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the session was used ({name})")

    # A lone surrogate cannot be a parameter value: the Allure plugin writes it to its UTF-8 JSON report and aborts the run.
    if isinstance(event_id, str):
        event_id = event_id.replace("<lone-surrogate>", "\ud800")
    with pytest.raises(ValueError, match=needle):
        process_once(
            Untouched(),  # type: ignore[arg-type]
            consumer=consumer,
            source=source,
            event_id=event_id,
            payload_hash=digest,
            apply=lambda: None,
        )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_time_defaults_to_now_and_a_naive_time_is_taken_as_utc(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        with factory() as session:
            process_once(
                session,
                consumer="billing",
                source="github",
                event_id="now",
                payload_hash=HASH_A,
                apply=lambda: None,
            )
            process_once(
                session,
                consumer="billing",
                source="github",
                event_id="naive",
                payload_hash=HASH_A,
                apply=lambda: None,
                now=T0.replace(tzinfo=None),
            )
            session.commit()
        with factory() as session:
            rows = {r.event_id: r for r in session.execute(select(CaliberInboxEvent)).scalars()}
        assert before <= rows["now"].processed_at <= datetime.now(timezone.utc).replace(tzinfo=None)
        assert rows["naive"].processed_at == T0.replace(tzinfo=None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_boundary_lengths_are_accepted(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            result = _take(session, "e" * 160, consumer="c" * 64, source="s" * 64)
            session.commit()
        assert result.outcome == PROCESSED


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_table_itself_refuses_a_second_row_for_one_identity_and_an_unknown_status(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        row = {"c": "billing", "s": "github", "e": "evt-1", "h": HASH_A}
        insert = text(
            "INSERT INTO caliber_inbox_events (consumer, source, event_id, payload_hash, status) "
            "VALUES (:c, :s, :e, :h, :st)"
        )
        with factory() as session:
            session.execute(insert, {**row, "st": "received"})
            session.commit()
        for params in ({**row, "st": "received"}, {**row, "e": "evt-2", "st": "done"}):
            with factory() as session, pytest.raises(IntegrityError):
                session.execute(insert, params)
        with factory() as session, pytest.raises(IntegrityError):
            session.execute(insert, {**row, "e": "evt-3", "h": "sha256:short", "st": "received"})


def _blind_first_read(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the session's first ``get`` see nothing, as if the row were committed just after the read."""
    real_get = session.get
    state = {"first": True}

    def blind(*args: Any, **kwargs: Any) -> Any:
        if state["first"]:
            state["first"] = False
            return None
        return real_get(*args, **kwargs)

    monkeypatch.setattr(session, "get", blind)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_losing_the_insert_race_reports_the_winner_as_a_duplicate_or_a_mismatch(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another transaction commits the same key between this one's read and its insert."""
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as other:
            _take(other)
            other.commit()
        for digest in (HASH_A, HASH_B):
            with factory() as session:
                _blind_first_read(session, monkeypatch)
                if digest == HASH_B:
                    with pytest.raises(InboxPayloadMismatch):
                        _take(session, digest=digest)
                else:
                    result = _take(session, digest=digest)
                    assert (result.outcome, result.calls) == (DUPLICATE, 0)
                session.rollback()
        assert _count(factory, CaliberProject) == 1


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_insert_that_fails_for_another_reason_than_a_taken_key_is_an_error(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory), factory() as session:
        real_flush = session.flush

        def failing(*args: Any, **kwargs: Any) -> None:
            if not any(isinstance(pending, CaliberInboxEvent) for pending in session.new):
                return real_flush(*args, **kwargs)  # not the reservation's insert
            monkeypatch.setattr(session, "flush", real_flush)
            raise IntegrityError("INSERT", {}, Exception("a constraint that is not the key"))

        monkeypatch.setattr(session, "flush", failing)
        with pytest.raises(IntegrityError, match="not the key"):
            _take(session)
        assert _count(factory, CaliberProject) == 0


@pytest.mark.parametrize("dialect", DIALECTS)
def test_prune_forgets_only_the_events_received_before_the_cutoff(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        with factory() as session:
            for number, age_days in enumerate((10, 8, 1)):
                process_once(
                    session,
                    consumer="billing",
                    source="github",
                    event_id=f"evt-{number}",
                    payload_hash=HASH_A,
                    apply=lambda: None,
                    now=T0 - timedelta(days=age_days),
                )
            session.commit()
        with factory() as session:
            assert prune(session, older_than=T0 - timedelta(days=7)) == 2
            session.commit()
        with factory() as session:
            assert set(session.execute(select(CaliberInboxEvent.event_id)).scalars()) == {"evt-2"}
            assert prune(session, older_than=T0 - timedelta(days=7)) == 0
            assert _take(session, "evt-0").outcome == PROCESSED, "a forgotten id is applied again"


# ---------------------------------------------------------------- concurrency (PostgreSQL)


def _need_postgresql() -> None:
    from tests.dialect_helpers import POSTGRES_TEST_URL

    if not POSTGRES_TEST_URL:
        pytest.skip("the race needs PostgreSQL")


def test_concurrent_duplicate_deliveries_apply_the_transition_once_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (_, factory):
        workers = 8
        barrier = threading.Barrier(workers)
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def deliver() -> None:
            try:
                with factory() as session:
                    barrier.wait(10)
                    result = _take(session)
                    session.commit()
                with lock:
                    outcomes.append(result.outcome)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=deliver, daemon=True) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert sorted(outcomes) == [DUPLICATE] * (workers - 1) + [PROCESSED]
        assert _count(factory, CaliberProject) == 1, "the business transition ran once"
        assert _count(factory, CaliberInboxEvent) == 1


def test_a_duplicate_waits_for_a_delivery_in_flight_and_takes_over_if_it_fails_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (engine, factory):
        entered = threading.Event()
        release = threading.Event()
        results: dict[str, Any] = {}

        def first() -> None:
            with factory() as session:

                def slow_then_fail() -> None:
                    entered.set()
                    release.wait(30)
                    raise RuntimeError("the first delivery fails after the second one arrived")

                try:
                    _take(session, apply=slow_then_fail)
                except RuntimeError as exc:
                    results["first"] = str(exc)
                    session.rollback()

        def second() -> None:
            with factory() as session:
                results["second"] = _take(session).outcome
                session.commit()

        a = threading.Thread(target=first, daemon=True)
        a.start()
        assert entered.wait(10)
        b = threading.Thread(target=second, daemon=True)
        b.start()
        deadline = time.monotonic() + 15
        while (
            time.monotonic() < deadline
        ):  # the second delivery is blocked on the first one's reservation
            with engine.connect() as probe:
                waiting = probe.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
                ).scalar_one()
            if waiting:
                break
            time.sleep(0.05)
        assert waiting, "the second delivery never waited on the first"
        assert "second" not in results, "it does not decide before the first one has"
        release.set()
        a.join(30)
        b.join(30)
        assert results["first"].startswith("the first delivery fails")
        assert results["second"] == PROCESSED, "the failed delivery left no reservation behind"
        assert _count(factory, CaliberProject) == 1
        assert _count(factory, CaliberInboxEvent) == 1


def test_a_different_payload_arriving_while_the_first_is_in_flight_is_refused_after_it_commits_on_postgresql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql()
    with _database("postgresql", tmp_path, monkeypatch) as (engine, factory):
        entered = threading.Event()
        release = threading.Event()
        results: dict[str, Any] = {}

        def first() -> None:
            with factory() as session:

                def slow() -> str:
                    session.add(CaliberProject(project_id="PRJ-slow", name="PRJ-slow", owner="@t"))
                    entered.set()
                    release.wait(30)
                    return "applied"

                results["first"] = _take(session, apply=slow).outcome
                session.commit()

        def second() -> None:
            with factory() as session:
                try:
                    _take(session, digest=HASH_B)
                except InboxPayloadMismatch:
                    results["second"] = "mismatch"
                session.rollback()

        a = threading.Thread(target=first, daemon=True)
        a.start()
        assert entered.wait(10)
        b = threading.Thread(target=second, daemon=True)
        b.start()
        deadline = time.monotonic() + 15
        waiting = 0
        while time.monotonic() < deadline and not waiting:
            with engine.connect() as probe:
                waiting = probe.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
                ).scalar_one()
            time.sleep(0.05)
        assert waiting and "second" not in results
        release.set()
        a.join(30)
        b.join(30)
        assert results == {"first": PROCESSED, "second": "mismatch"}
        assert _count(factory, CaliberProject) == 1
        with factory() as session:
            assert session.execute(select(CaliberInboxEvent.payload_hash)).scalar_one() == HASH_A
