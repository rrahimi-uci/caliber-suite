"""Durable calibration: submit, claim, execute, poll.

Calibration scores every saved test case through the sandbox. With up to 200 cases each
paying a cold subprocess start that is minutes of work, and it used to be one synchronous
HTTP request. An earlier pass fixed the sharp edges — the waits blocked the event loop, and
a session was held across execution — but the shape remained: the client holds a connection
open for the whole run, a proxy timeout or a closed lid loses the result, and nothing
recorded that the work happened.

The properties worth pinning are the ones that make it *durable* rather than merely
asynchronous: exactly one drain runs a job, an outcome always exists even when the work
fails, and a result is never attributed to a tool definition that did not produce it.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from caliber.orchestrator import calibration_drain


def _job(
    session: Any,
    tool_id: str = "TL-1",
    cases: list[dict[str, Any]] | None = None,
    *,
    tool_snapshot: dict[str, Any] | None = None,
) -> str:
    from caliber.db.models import CaliberCalibrationJob

    job = CaliberCalibrationJob(
        job_id=f"CAL-{tool_id}-{len(cases or [])}",
        tool_id=tool_id,
        status=calibration_drain.STATUS_QUEUED,
        requested_by="@tester",
        tool_snapshot=tool_snapshot,
        test_cases=cases if cases is not None else [{"input": {}, "assertion": {}}],
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(job)
    session.commit()
    return job.job_id


def _tool(
    session: Any,
    *,
    tool_id: str = "TL-1",
    cases: list[dict[str, Any]] | None = None,
) -> Any:
    from caliber.db.models import CaliberToolRegistry

    row = CaliberToolRegistry(
        tool_id=tool_id,
        name=f"calibration_{tool_id}",
        version="1.0",
        description="submitted definition",
        module_path="caliber.workflows.demo_tools",
        callable_name="lookup_policy",
        side_effect_level="read",
        requires_approval=False,
        allow_in_preview=True,
        secret_refs=[],
        test_cases=cases or [{"name": "c1", "input": {}, "assertion": {}}],
        owner="@tester",
        status="active",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_a_claimed_job_is_not_claimable_again(session_factory) -> None:
    """Sequential drains: the second finds nothing queued."""
    with session_factory() as session:
        job_id = _job(session)

    with session_factory() as first, session_factory() as second:
        won = calibration_drain.claim_next_job(first, claimed_by="host:1")
        lost = calibration_drain.claim_next_job(second, claimed_by="host:2")

    assert won is not None and won.job_id == job_id
    assert lost is None


def test_two_drains_racing_on_the_same_row_cannot_both_win(session_factory) -> None:
    """The conditional UPDATE is the real arbitration, exercised through the real function.

    Two weaker versions of this test were written first and both proved nothing. Calling
    ``claim_next_job`` twice in sequence is useless because the second drain's ``SELECT``
    already excludes the row. Issuing the ``UPDATE`` directly from the test is worse: it
    hardcodes the predicate in the test, so it passes even when production stops using one.
    Both survived deleting the ``status`` predicate from ``claim_next_job``.

    The dangerous interleaving is both drains selecting the same queued row *before* either
    updates it. This reproduces it by letting a competitor claim the row in the window
    between this drain's SELECT and its UPDATE, then asserting this drain comes away with
    nothing. It fails if the predicate is removed.

    It matters because calibration invokes tools, so running one job twice is not a
    harmless duplicate.
    """
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        job_id = _job(session)

    class _RacingSession:
        """Delegates to a real session, but lets a competitor win between SELECT and UPDATE."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._selected = False

        def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = self._inner.execute(statement, *args, **kwargs)
            if not self._selected:
                self._selected = True
                # End this session's read transaction so SQLite lets the rival write. The
                # stale candidate is already in hand, which is precisely the situation
                # being modelled: a drain about to UPDATE a row someone else just took.
                self._inner.commit()
                with session_factory() as rival:
                    calibration_drain.claim_next_job(rival, claimed_by="rival")
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    with session_factory() as inner:
        loser = calibration_drain.claim_next_job(_RacingSession(inner), claimed_by="slow")

    assert loser is None, "a drain whose row was claimed mid-flight must not also run it"
    with session_factory() as session:
        assert session.get(CaliberCalibrationJob, job_id).claimed_by == "rival"


def test_claiming_records_which_process_took_it(session_factory) -> None:
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        job_id = _job(session)
    with session_factory() as session:
        calibration_drain.claim_next_job(session, claimed_by="host-a:99")
        row = session.get(CaliberCalibrationJob, job_id)

        assert row.status == calibration_drain.STATUS_RUNNING
        assert row.claimed_by == "host-a:99"
        assert row.claimed_at is not None


def test_an_empty_queue_claims_nothing(session_factory) -> None:
    with session_factory() as session:
        assert calibration_drain.claim_next_job(session, claimed_by="host:1") is None


def test_a_legacy_job_without_a_tool_snapshot_fails_clearly(session_factory) -> None:
    """An outcome must always exist. A drain that raised would stop every later
    calibration, and a job with no result is indistinguishable from one never run."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        job_id = _job(session, tool_id="TL-does-not-exist")
    with session_factory() as session:
        calibration_drain.claim_next_job(session, claimed_by="host:1")
    calibration_drain.run_job(session_factory, job_id, config=None)
    with session_factory() as session:
        row = session.get(CaliberCalibrationJob, job_id)

    assert row.status == calibration_drain.STATUS_FAILED
    assert "no captured tool snapshot" in (row.error or "")
    assert row.finished_at is not None


def test_a_job_with_no_captured_cases_fails_clearly(session_factory) -> None:
    """A distinct failure from "the tool vanished", so the two are diagnosable apart."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        # The tool must exist, or execution fails on the earlier check and this would
        # assert nothing about empty cases.
        tool = _tool(session, cases=[{"name": "saved", "input": {}, "assertion": {}}])
        job_id = _job(session, cases=[], tool_snapshot=calibration_drain.snapshot_tool(tool))
    with session_factory() as session:
        calibration_drain.claim_next_job(session, claimed_by="host:1")
    calibration_drain.run_job(session_factory, job_id, config=None)
    with session_factory() as session:
        row = session.get(CaliberCalibrationJob, job_id)

    assert row.status == calibration_drain.STATUS_FAILED
    assert "no test cases" in (row.error or "")


def test_submitting_returns_202_and_the_job_is_pollable(client) -> None:
    """The whole point: the caller gets an id immediately instead of holding a connection
    open for the length of the run."""
    from tests.workflow_helpers import PREFIX

    create = client.post(
        f"{PREFIX}/tools",
        json={
            "name": "calib_probe",
            "version": "1.0",
            "description": "probe",
            "module_path": "caliber.workflows.demo_tools",
            "callable_name": "lookup_policy",
            "side_effect_level": "read",
            "allow_in_preview": True,
        },
    )
    assert create.status_code == 201, create.text
    tool_id = create.json()["data"]["tool_id"]

    cases = [{"name": "c1", "input": {"query": "refund"}, "assertion": {"type": "no_error"}}]
    saved = client.put(f"{PREFIX}/tools/{tool_id}/test-cases", json={"test_cases": cases})
    assert saved.status_code == 200, saved.text

    submitted = client.post(f"{PREFIX}/tools/{tool_id}/calibration-jobs", json={})
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["data"]["job_id"]
    assert submitted.json()["data"]["status"] == "queued"

    from caliber.db.models import CaliberCalibrationJob

    with client.app.state.session_factory() as session:
        row = session.get(CaliberCalibrationJob, job_id)
        assert row.tool_snapshot["tool_id"] == tool_id
        assert row.tool_snapshot["module_path"] == "caliber.workflows.demo_tools"
        row.status = calibration_drain.STATUS_RUNNING
        row.claimed_at = datetime(2026, 7, 30, 12, 0, 0)
        row.claimed_by = "worker-a:42"
        session.commit()

    polled = client.get(f"{PREFIX}/tools/{tool_id}/calibration-jobs/{job_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["data"]["job_id"] == job_id
    assert polled.json()["data"]["claimed_by"] == "worker-a:42"
    assert polled.json()["data"]["claimed_at"] == "2026-07-30T12:00:00"

    listed = client.get(f"{PREFIX}/tools/{tool_id}/calibration-jobs")
    assert listed.status_code == 200
    assert [j["job_id"] for j in listed.json()["data"]["jobs"]] == [job_id]
    assert listed.json()["data"]["jobs"][0]["claimed_by"] == "worker-a:42"


def test_submitting_without_saved_cases_is_400(client) -> None:
    """Queueing work that cannot produce a result would leave a job that always fails."""
    from tests.workflow_helpers import PREFIX

    create = client.post(
        f"{PREFIX}/tools",
        json={
            "name": "no_cases_probe",
            "version": "1.0",
            "description": "probe",
            "module_path": "caliber.workflows.demo_tools",
            "callable_name": "lookup_policy",
            "side_effect_level": "read",
            "allow_in_preview": True,
        },
    )
    tool_id = create.json()["data"]["tool_id"]

    response = client.post(f"{PREFIX}/tools/{tool_id}/calibration-jobs", json={})

    assert response.status_code == 400, response.text
    assert "no saved test cases" in response.json()["detail"]


def test_a_job_id_does_not_bypass_tool_visibility(client, db_session) -> None:
    """Every child in this family is scoped through its parent tool. A job id must not be
    a way to read a calibration for a tool the caller cannot see."""
    from caliber.db.models import CaliberToolRegistry
    from tests.workflow_helpers import PREFIX

    client.app.state.config = client.app.state.config.model_copy(update={"operator_users": "@dev2"})
    db_session.add(
        CaliberToolRegistry(
            tool_id="TL-hidden",
            name="hidden_tool",
            version="1.0",
            module_path="caliber.workflows.demo_tools",
            callable_name="lookup_policy",
            owner="@someone",
            project_id="PRJ-MINE",
            visibility="project",
            status="active",
        )
    )
    db_session.commit()
    job_id = _job(db_session, tool_id="TL-hidden")

    other = {"X-CALIBER-User": "@dev2", "X-CALIBER-Project": "PRJ-OTHER"}
    detail = client.get(f"{PREFIX}/tools/TL-hidden/calibration-jobs/{job_id}", headers=other)
    listed = client.get(f"{PREFIX}/tools/TL-hidden/calibration-jobs", headers=other)

    assert detail.status_code == 404, detail.text
    assert listed.status_code == 404, listed.text


@pytest.mark.asyncio
async def test_the_drain_executes_a_queued_job(session_factory) -> None:
    """End-to-end through the drain, so the queue is not a table nothing reads."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        job_id = _job(session, tool_id="TL-missing-for-drain")

    drain = calibration_drain.CalibrationDrain(session_factory, config=None)
    ran = await drain.tick()

    with session_factory() as session:
        row = session.get(CaliberCalibrationJob, job_id)

    assert ran == 1, "the drain must pick up a queued job"
    # The tool does not exist, so it fails — but it *ran*, which is the property here.
    assert row.status == calibration_drain.STATUS_FAILED
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_the_drain_is_a_no_op_when_the_queue_is_empty(session_factory) -> None:
    drain = calibration_drain.CalibrationDrain(session_factory, config=None)
    assert await drain.tick() == 0


def test_execution_holds_no_database_session_across_scoring(session_factory, monkeypatch) -> None:
    """A pool connection must be available while the sandbox subprocess is running."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        tool = _tool(session)
        job_id = _job(
            session,
            tool_snapshot=calibration_drain.snapshot_tool(tool),
            cases=list(tool.test_cases),
        )
    with session_factory() as session:
        calibration_drain.claim_next_job(session, claimed_by="host:1")

    class _TrackedFactory:
        active = 0

        def __call__(self):
            @contextmanager
            def _open():
                self.active += 1
                try:
                    with session_factory() as session:
                        yield session
                finally:
                    self.active -= 1

            return _open()

    tracked = _TrackedFactory()

    def _score(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        assert tracked.active == 0, "the sandbox ran while a DB session was checked out"
        return [{"passed": True}]

    monkeypatch.setattr("caliber.routes.tools._score_tool_cases", _score)
    calibration_drain.run_job(tracked, job_id, config=None)

    with session_factory() as session:
        assert session.get(CaliberCalibrationJob, job_id).status == "completed"


def test_definition_and_case_drift_marks_the_snapshot_result_stale(
    session_factory, monkeypatch
) -> None:
    """Concurrent edits cannot relabel a submitted snapshot's result as current."""
    from caliber.db.models import CaliberCalibrationJob, CaliberToolRegistry

    submitted_cases = [{"name": "original", "input": {}, "assertion": {}}]
    with session_factory() as session:
        tool = _tool(session, cases=submitted_cases)
        submitted_snapshot = calibration_drain.snapshot_tool(tool)
        job_id = _job(
            session,
            cases=submitted_cases,
            tool_snapshot=submitted_snapshot,
        )
    with session_factory() as session:
        calibration_drain.claim_next_job(session, claimed_by="host:1")

    def _score(tool_data: Any, cases: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del kwargs
        assert tool_data.description == "submitted definition"
        assert cases == submitted_cases
        with session_factory() as concurrent:
            current = concurrent.get(CaliberToolRegistry, "TL-1")
            current.description = "definition edited while running"
            current.test_cases = [{"name": "replacement", "input": {}, "assertion": {}}]
            concurrent.commit()
        return [{"passed": True}]

    monkeypatch.setattr("caliber.routes.tools._score_tool_cases", _score)
    calibration_drain.run_job(session_factory, job_id, config=None)

    with session_factory() as session:
        job = session.get(CaliberCalibrationJob, job_id)
        current = session.get(CaliberToolRegistry, "TL-1")
        assert job.status == "completed"
        assert job.result["stale"] is True
        assert set(job.result["stale_reasons"]) == {
            "tool_definition_changed",
            "test_cases_changed",
        }
        assert current.last_calibration is None


def test_mutation_unconditionally_clears_a_result_attached_after_its_read(
    session_factory,
) -> None:
    """A stale ORM ``None`` must not suppress the database NULL assignment.

    This is the exact former race: the editor read no calibration, a worker attached one,
    then the editor committed a new definition. Python ``None -> None`` was not dirty, so
    the old result survived on the new revision.
    """
    from caliber.db.models import CaliberToolRegistry

    cases = [{"name": "original", "input": {}, "assertion": {}}]
    result = {"pass_rate": 1.0, "passed": 1, "failed": 0, "total": 1}
    with session_factory() as setup:
        tool = _tool(setup, cases=cases)
        submitted = calibration_drain.snapshot_tool(tool)

    with session_factory() as mutation:
        edited = mutation.get(CaliberToolRegistry, "TL-1")
        assert edited.last_calibration is None
        edited.description = "definition edited after worker submission"

        with session_factory() as worker:
            assert (
                calibration_drain.attach_calibration_if_current(
                    worker, "TL-1", submitted, cases, result
                )
                == []
            )
            worker.commit()

        calibration_drain.invalidate_tool_calibration(mutation, edited)
        mutation.commit()

    with session_factory() as verify:
        current = verify.get(CaliberToolRegistry, "TL-1")
        assert current.calibration_revision == 2
        assert current.description == "definition edited after worker submission"
        assert current.last_calibration is None


def test_concurrent_editors_advance_revision_from_the_database_value(session_factory) -> None:
    """Two stale ORM snapshots must produce two revision increments, not one."""
    from caliber.db.models import CaliberToolRegistry

    with session_factory() as setup:
        _tool(setup)

    with session_factory() as first, session_factory() as second:
        first_tool = first.get(CaliberToolRegistry, "TL-1")
        second_tool = second.get(CaliberToolRegistry, "TL-1")
        assert first_tool.calibration_revision == second_tool.calibration_revision == 1

        first_tool.description = "first edit"
        calibration_drain.invalidate_tool_calibration(first, first_tool)
        first.commit()

        second_tool.description = "second edit"
        calibration_drain.invalidate_tool_calibration(second, second_tool)
        second.commit()

    with session_factory() as verify:
        current = verify.get(CaliberToolRegistry, "TL-1")
        assert current.calibration_revision == 3
        assert current.last_calibration is None


@pytest.mark.asyncio
async def test_stop_is_bounded_and_a_late_worker_cannot_touch_the_database(
    session_factory, monkeypatch
) -> None:
    """A long case cannot hold shutdown open or attach an outcome after its fence."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        tool = _tool(session)
        job_id = _job(
            session,
            tool_snapshot=calibration_drain.snapshot_tool(tool),
            cases=list(tool.test_cases),
        )

    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    drain = calibration_drain.CalibrationDrain(session_factory, config=None, interval_seconds=60.0)

    def _score(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        entered.set()
        try:
            release.wait(timeout=5.0)
            return [{"passed": True}]
        finally:
            worker_finished.set()

    monkeypatch.setattr("caliber.routes.tools._score_tool_cases", _score)
    await drain.start()
    deadline = asyncio.get_running_loop().time() + 5.0
    while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert entered.is_set(), "the drain never began the queued job"

    started = asyncio.get_running_loop().time()
    try:
        await drain.stop(grace_seconds=0.05)
        assert asyncio.get_running_loop().time() - started < 0.5

        with session_factory() as session:
            row = session.get(CaliberCalibrationJob, job_id)
            assert row.status == calibration_drain.STATUS_RUNNING
            assert row.error is None
            assert row.finished_at is None

        # The executor thread outlives the cancelled asyncio await. Once released it
        # observes its generation-specific stop signal and returns without a terminal
        # write, so engine disposal after stop remains safe.
        release.set()
        deadline = asyncio.get_running_loop().time() + 5.0
        while not worker_finished.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert worker_finished.is_set()
        await asyncio.sleep(0.05)

        with session_factory() as session:
            row = session.get(CaliberCalibrationJob, job_id)
            assert row.status == calibration_drain.STATUS_RUNNING
            assert row.error is None
            assert row.finished_at is None
            assert row.result is None
    finally:
        release.set()


@pytest.mark.asyncio
async def test_stop_never_launches_a_late_database_settlement(session_factory, monkeypatch) -> None:
    """No detached settlement thread may touch the engine after stop returns."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        tool = _tool(session)
        job_id = _job(
            session,
            tool_snapshot=calibration_drain.snapshot_tool(tool),
            cases=list(tool.test_cases),
        )

    entered = threading.Event()
    release_score = threading.Event()
    worker_finished = threading.Event()

    def _score(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        entered.set()
        try:
            release_score.wait(timeout=5.0)
            return [{"passed": True}]
        finally:
            worker_finished.set()

    class _CountingFactory:
        def __init__(self) -> None:
            self.opens = 0
            self.lock = threading.Lock()

        def __call__(self):
            @contextmanager
            def _open():
                with self.lock:
                    self.opens += 1
                with session_factory() as session:
                    yield session

            return _open()

    monkeypatch.setattr("caliber.routes.tools._score_tool_cases", _score)
    tracked = _CountingFactory()
    drain = calibration_drain.CalibrationDrain(tracked, config=None, interval_seconds=60.0)
    await drain.start()
    deadline = asyncio.get_running_loop().time() + 5.0
    while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert entered.is_set()
    opens_before_stop = tracked.opens

    started = asyncio.get_running_loop().time()
    try:
        await drain.stop(grace_seconds=0.05)
        assert asyncio.get_running_loop().time() - started < 0.5
    finally:
        release_score.set()

    # The removed implementation scheduled a second `to_thread` database settlement here.
    # Its coroutine could time out while the executor thread started later, after lifespan
    # disposal. Waiting on both sides of scorer release proves no such session appears.
    await asyncio.sleep(0.1)
    assert tracked.opens == opens_before_stop
    deadline = asyncio.get_running_loop().time() + 5.0
    while not worker_finished.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert worker_finished.is_set()
    await asyncio.sleep(0.05)
    assert tracked.opens == opens_before_stop

    with session_factory() as session:
        row = session.get(CaliberCalibrationJob, job_id)
        assert row.status == calibration_drain.STATUS_RUNNING
        assert row.error is None
        assert row.finished_at is None
        assert row.result is None


@pytest.mark.asyncio
async def test_stop_fences_a_generation_while_claim_is_wedged(session_factory, monkeypatch) -> None:
    """A database claim stuck after commit cannot make shutdown unbounded or run late."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        job_id = _job(session, tool_id="TL-late-claim")

    drain = calibration_drain.CalibrationDrain(
        session_factory,
        config=None,
        interval_seconds=60.0,
    )
    real_claim = drain._claim_one
    claim_committed = threading.Event()
    release_claim = threading.Event()
    claim_finished = threading.Event()
    executions: list[str] = []

    def _claim_then_wedge() -> str | None:
        claimed = real_claim()
        claim_committed.set()
        try:
            release_claim.wait(timeout=5.0)
            return claimed
        finally:
            claim_finished.set()

    monkeypatch.setattr(drain, "_claim_one", _claim_then_wedge)
    monkeypatch.setattr(
        drain,
        "_execute",
        lambda claimed_id, _generation: executions.append(claimed_id),
    )
    await drain.start()
    deadline = asyncio.get_running_loop().time() + 5.0
    while not claim_committed.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert claim_committed.is_set(), "claim did not reach its simulated database wedge"

    stop_task = asyncio.create_task(drain.stop(grace_seconds=0.05))
    await asyncio.sleep(0)
    assert drain._stop_requested.is_set(), "the generation fence must be immediate"
    started = asyncio.get_running_loop().time()
    try:
        await stop_task
        assert asyncio.get_running_loop().time() - started < 0.5
    finally:
        release_claim.set()

    deadline = asyncio.get_running_loop().time() + 5.0
    while not claim_finished.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert claim_finished.is_set(), "the executor thread did not leave the test wedge"
    await asyncio.sleep(0.05)

    assert executions == [], "a claim returned after its generation fence must never execute"
    with session_factory() as session:
        row = session.get(CaliberCalibrationJob, job_id)
        # The commit happened before the simulated driver wedge, so it remains explicit
        # ambiguous evidence. What must never appear is a late terminal outcome.
        assert row.status == calibration_drain.STATUS_RUNNING
        assert row.result is None
        assert row.error is None
