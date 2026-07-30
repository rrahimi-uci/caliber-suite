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

from datetime import datetime, timezone
from typing import Any

import pytest

from caliber.orchestrator import calibration_drain


def _job(session: Any, tool_id: str = "TL-1", cases: list[dict[str, Any]] | None = None) -> str:
    from caliber.db.models import CaliberCalibrationJob

    job = CaliberCalibrationJob(
        job_id=f"CAL-{tool_id}-{len(cases or [])}",
        tool_id=tool_id,
        status=calibration_drain.STATUS_QUEUED,
        requested_by="@tester",
        test_cases=cases if cases is not None else [{"input": {}, "assertion": {}}],
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(job)
    session.commit()
    return job.job_id


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


def test_a_failing_job_records_the_failure_rather_than_raising(session_factory) -> None:
    """An outcome must always exist. A drain that raised would stop every later
    calibration, and a job with no result is indistinguishable from one never run."""
    from caliber.db.models import CaliberCalibrationJob

    with session_factory() as session:
        # No such tool, so execution cannot succeed.
        job_id = _job(session, tool_id="TL-does-not-exist")
    with session_factory() as session:
        job = calibration_drain.claim_next_job(session, claimed_by="host:1")
        calibration_drain.run_job(session, job, config=None)
        row = session.get(CaliberCalibrationJob, job_id)

    assert row.status == calibration_drain.STATUS_FAILED
    assert "no longer exists" in (row.error or "")
    assert row.finished_at is not None


def test_a_job_with_no_captured_cases_fails_clearly(session_factory) -> None:
    """A distinct failure from "the tool vanished", so the two are diagnosable apart."""
    from caliber.db.models import CaliberCalibrationJob, CaliberToolRegistry

    with session_factory() as session:
        # The tool must exist, or execution fails on the earlier check and this would
        # assert nothing about empty cases.
        session.add(
            CaliberToolRegistry(
                tool_id="TL-1",
                name="empty_cases_probe",
                version="1.0",
                module_path="caliber.workflows.demo_tools",
                callable_name="lookup_policy",
                owner="@tester",
                status="active",
            )
        )
        session.commit()
        job_id = _job(session, cases=[])
    with session_factory() as session:
        job = calibration_drain.claim_next_job(session, claimed_by="host:1")
        calibration_drain.run_job(session, job, config=None)
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

    polled = client.get(f"{PREFIX}/tools/{tool_id}/calibration-jobs/{job_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["data"]["job_id"] == job_id

    listed = client.get(f"{PREFIX}/tools/{tool_id}/calibration-jobs")
    assert listed.status_code == 200
    assert [j["job_id"] for j in listed.json()["data"]["jobs"]] == [job_id]


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
