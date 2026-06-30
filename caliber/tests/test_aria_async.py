"""Phase 4 (part 2) — async executor: waiting_job parking, poll, resume."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.assistant import capabilities as caps
from caliber.assistant.capabilities import Capability
from caliber.assistant.executor import (
    AsyncJobHandle,
    FakeJobStatusResolver,
    JobStatus,
    MLflowJobStatusResolver,
    PlanExecutor,
)
from caliber.assistant.plans import PlannedStep, PlanService
from caliber.config import CaliberConfig
from caliber.db.models import CaliberAriaPlan, CaliberAriaPlanStep
from caliber.ids import new_aria_plan_id, new_aria_plan_step_id

# Plan owner (@reza) holds operator so the executor's per-capability scope floor
# lets the real workflow.calibrate (operator-tier) step run.
_CFG = CaliberConfig(operator_users="@reza")
_JOB_ID = "JOB-async-1"


@pytest.fixture
def async_cap():
    """Register a transient async capability (returns an AsyncJobHandle)."""
    cap = Capability(
        key="test.async_job",
        title="Async job",
        description="enqueues a job",
        tier="mutate",
        handler=lambda _ctx, _args: AsyncJobHandle(job_id=_JOB_ID, kind="fake"),
    )
    caps.register(cap)
    try:
        yield cap
    finally:
        caps.unregister("test.async_job")


class _AsyncPlanner:
    def plan(self, goal, *, capabilities):
        return [PlannedStep(capability_key="test.async_job", title="Async job")]


def _approved_async_plan(session_factory) -> str:
    svc = PlanService(planner=_AsyncPlanner())
    detail = svc.create_plan(
        session_factory=session_factory, goal="run async", owner="@reza", autonomy="approve_plan"
    )
    plan_id = detail["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    return plan_id


def test_execute_parks_async_step(async_cap, session_factory) -> None:
    plan_id = _approved_async_plan(session_factory)
    detail = PlanExecutor().execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    # Job in flight → step waiting_job, plan stays running (not completed).
    assert detail["plan"]["status"] == "running"
    assert detail["steps"][0]["status"] == "waiting_job"
    assert detail["steps"][0]["job_id"] == _JOB_ID


def test_poll_pending_leaves_waiting(async_cap, session_factory) -> None:
    plan_id = _approved_async_plan(session_factory)
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    detail = ex.poll(
        session_factory=session_factory,
        config=_CFG,
        plan_id=plan_id,
        resolver=FakeJobStatusResolver(),  # job pending
    )
    assert detail["plan"]["status"] == "running"
    assert detail["steps"][0]["status"] == "waiting_job"


def test_poll_done_completes_plan_with_evidence(async_cap, session_factory) -> None:
    plan_id = _approved_async_plan(session_factory)
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    resolver = FakeJobStatusResolver({_JOB_ID: JobStatus(state="done", evidence={"score": 0.9})})
    detail = ex.poll(
        session_factory=session_factory, config=_CFG, plan_id=plan_id, resolver=resolver
    )
    assert detail["plan"]["status"] == "completed"
    assert detail["steps"][0]["status"] == "done"
    assert detail["steps"][0]["evidence"] == {"score": 0.9}


def test_poll_failed_fails_plan(async_cap, session_factory) -> None:
    plan_id = _approved_async_plan(session_factory)
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    resolver = FakeJobStatusResolver({_JOB_ID: JobStatus(state="failed", error="boom")})
    detail = ex.poll(
        session_factory=session_factory, config=_CFG, plan_id=plan_id, resolver=resolver
    )
    assert detail["plan"]["status"] == "failed"
    assert detail["steps"][0]["status"] == "failed"
    assert "boom" in (detail["steps"][0]["error"] or "")


def test_poll_waiting_plans_finds_in_flight_plan(async_cap, session_factory) -> None:
    plan_id = _approved_async_plan(session_factory)
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    polled = ex.poll_waiting_plans(
        session_factory=session_factory, config=_CFG, resolver=FakeJobStatusResolver()
    )
    assert plan_id in polled


# --- MLflowJobStatusResolver (no-seed paths) --------------------------------


def test_resolver_unknown_kind_is_pending(session_factory) -> None:
    status = MLflowJobStatusResolver().resolve(
        kind="nope", job_id="x", session_factory=session_factory
    )
    assert status.state == "pending"


def test_resolver_missing_job_is_failed(session_factory) -> None:
    status = MLflowJobStatusResolver().resolve(
        kind="refinement_job", job_id="RFN-missing", session_factory=session_factory
    )
    assert status.state == "failed"


# --- route ------------------------------------------------------------------

_PLAN_API = "/ajax-api/2.0/mlflow/caliber/aria/plans"


def test_route_poll_missing_job_fails_step(client: TestClient, db_session: Session) -> None:
    # Seed a plan parked on a refinement job that doesn't exist → poll fails it.
    plan_id = new_aria_plan_id()
    db_session.add(
        CaliberAriaPlan(
            plan_id=plan_id, goal="g", status="running", autonomy="approve_plan", owner="@test"
        )
    )
    db_session.add(
        CaliberAriaPlanStep(
            step_id=new_aria_plan_step_id(),
            plan_id=plan_id,
            seq=0,
            capability_key="test.async_job",
            title="Async",
            inputs={},
            depends_on=[],
            status="waiting_job",
            job_id="RFN-nope",
            result={"__job_kind__": "refinement_job"},
        )
    )
    db_session.commit()
    resp = client.post(f"{_PLAN_API}/{plan_id}/poll")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["plan"]["status"] == "failed"


# --- background worker (tick) ------------------------------------------------


def test_plan_worker_tick_resumes_done_jobs(async_cap, session_factory) -> None:
    from caliber.assistant.plan_worker import AriaPlanWorker

    plan_id = _approved_async_plan(session_factory)
    PlanExecutor().execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    resolver = FakeJobStatusResolver({_JOB_ID: JobStatus(state="done")})
    worker = AriaPlanWorker(session_factory, config=_CFG, interval_seconds=0.01, resolver=resolver)
    polled = worker.tick()  # one synchronous poll pass
    assert plan_id in polled
    with session_factory() as db:
        plan = db.get(CaliberAriaPlan, plan_id)
        assert plan.status == "completed"


# --- real async capability (workflow.calibrate) -----------------------------


def test_real_calibrate_capability_parks_then_resumes(session_factory, monkeypatch) -> None:
    """The real workflow.calibrate capability enqueues a job, parks, and resumes."""
    from types import SimpleNamespace

    # Stub the heavy enqueue (real preconditions: workflow/agent/baseline/dataset)
    # so the test exercises the capability wiring + async park, not the pipeline.
    monkeypatch.setattr(
        "caliber.routes.workflow_calibration.enqueue_workflow_calibration_run",
        lambda **_kw: SimpleNamespace(
            job=SimpleNamespace(job_id="RFN-fake"), item=SimpleNamespace(item_id="FB-fake")
        ),
    )

    class _CalibPlanner:
        def plan(self, goal, *, capabilities):
            return [
                PlannedStep(
                    capability_key="workflow.calibrate",
                    title="Calibrate",
                    inputs={"workflow_id": "WF-1", "agent_id": "@agent"},
                )
            ]

    svc = PlanService(planner=_CalibPlanner())
    plan_id = svc.create_plan(
        session_factory=session_factory, goal="calibrate", owner="@reza", autonomy="approve_plan"
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    ex = PlanExecutor()
    detail = ex.execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    assert detail["plan"]["status"] == "running"
    assert detail["steps"][0]["status"] == "waiting_job"
    assert detail["steps"][0]["job_id"] == "RFN-fake"

    # Resolve the job as completed → the plan resumes to completion.
    detail2 = ex.poll(
        session_factory=session_factory,
        config=_CFG,
        plan_id=plan_id,
        resolver=FakeJobStatusResolver({"RFN-fake": JobStatus(state="done")}),
    )
    assert detail2["plan"]["status"] == "completed"
    assert detail2["steps"][0]["status"] == "done"


# ---------------------------------------------------------------------------
# Regression (#2): MLflowJobStatusResolver must recognize each kind's OWN
# terminal vocabulary. Refinement jobs never use "completed" — mapping only
# "completed"->done made workflow.calibrate plans hang forever.
# ---------------------------------------------------------------------------


class _FakeRow:
    def __init__(self, status: str) -> None:
        self.status = status


def _resolver_factory(status: str):
    """A session_factory whose session.get() returns a row with ``status``."""

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def get(self, _model: object, _job_id: str) -> _FakeRow:
            return _FakeRow(status)

    return lambda: _Session()


def test_refinement_job_candidate_ready_resolves_done() -> None:
    res = MLflowJobStatusResolver().resolve(
        kind="refinement_job", job_id="RFN-1", session_factory=_resolver_factory("candidate_ready")
    )
    assert res.state == "done"


def test_refinement_job_applied_resolves_done() -> None:
    res = MLflowJobStatusResolver().resolve(
        kind="refinement_job", job_id="RFN-2", session_factory=_resolver_factory("applied")
    )
    assert res.state == "done"


def test_refinement_job_rejected_resolves_failed() -> None:
    res = MLflowJobStatusResolver().resolve(
        kind="refinement_job", job_id="RFN-3", session_factory=_resolver_factory("rejected")
    )
    assert res.state == "failed"


def test_refinement_job_running_resolves_pending() -> None:
    res = MLflowJobStatusResolver().resolve(
        kind="refinement_job", job_id="RFN-4", session_factory=_resolver_factory("running")
    )
    assert res.state == "pending"


def test_eval_run_completed_resolves_done() -> None:
    res = MLflowJobStatusResolver().resolve(
        kind="eval_run", job_id="EVR-1", session_factory=_resolver_factory("completed")
    )
    assert res.state == "done"
