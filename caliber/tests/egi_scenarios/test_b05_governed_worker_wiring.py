"""#1051 on PostgreSQL: a queued run's protected effect crosses the governed path end to end, by the public API only.

The reference-profile app. A project editor admits a run of a workflow whose agent node is bound to a tool the operator mapped
to the ``document_request`` backend; the worker (this process, under an execution-owner lease it acquired) runs the agent as a
governed step with a scripted model. The model *proposes* a protected action; nothing is sent. The run parks ``waiting_approval``
behind the approval request bound to that exact intent, a different person approves it over HTTP, an operator resumes the run over
HTTP, and the worker's next pass dispatches it: the dispatcher consumes the approval and claims the dispatch before the send, the
downstream counts exactly one mutation, the receipt is read back by the connector's own lookup by key, and the run completes.

Measured at the downstream (``downstream.mutations()``), never in CALIBER's logs. Around the happy path: an approver whose project
role is revoked after deciding dispatches nothing; a worker with no lease, or one that was never wired to the lease, dispatches
nothing (the second never even asks the model); the development profile keeps its own behavior; and a worker killed at the two
moments that matter (after the claim, before the send; after the downstream committed, before the outcome was recorded) never
causes a second mutation: the new owner's reconciler asks the downstream by key and the run completes with the one effect.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from caliber.config import CaliberConfig
from caliber.contracts.agent_step import ProposedAction
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.callbacks import parse_bindings
from caliber.effects.reconcile import (
    RECONCILED_COMMITTED,
    STILL_UNKNOWN,
    ask_downstream,
    reconcile_pass,
    targets_from_bindings,
)
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy
from caliber.orchestrator import workflow_run_worker as worker_module
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.ownership import Lease, acquire, current_lease, release
from caliber.workflows.agent_step import ScriptedStep, StepUsage
from caliber.workflows.runtime import FakeWorkflowExecutor
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    PROJECT_A,
    Downstream,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.workflow_helpers import make_manifest, make_tool_payload

CASE = "MORT-2026-000184"
DOCUMENT = "pay_statement"
PROTECTED = "request_missing_document"
NODE = "agent"
CALIBER_DIR = Path(__file__).resolve().parents[2]
OWNER = "egi-owner-1"
ADMIN = "egi_admin"  # registering a tool needs the admin scope; the operator lists are the scenario's, this one is the registrar's
LEASE_SECONDS = 600.0


def _reference_only(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip(
            "the governed path is the reference profile's, and that profile needs PostgreSQL"
        )


def _backends() -> str:
    return json.dumps([{"tool": PROTECTED, "version": "1.0", "backend": "document_request"}])


def _bindings(downstream: Downstream) -> str:
    return json.dumps(
        [
            {
                "connector": "document-request",
                "project_id": PROJECT_A,
                "secret_source": "env://EGI_CALLBACK_SECRET",
                "base_url": downstream.base_url,
            }
        ]
    )


def worker_config(app: ReferenceApp, downstream: Downstream, **changes: Any) -> CaliberConfig:
    """The configuration of the worker process: the app's, plus the operator's two trusted maps."""
    update_fields: dict[str, Any] = {
        "workflow_run_queue_enabled": True,
        "workflow_run_runtime_approvals_enabled": True,
        "workflow_run_checkpointing_enabled": True,
        "admission_enabled": True,
        "admin_users": ADMIN,
        "agent_step_tool_backends": _backends(),
        "effect_callback_connectors": _bindings(downstream),
        **changes,
    }
    return app.config.model_copy(update=update_fields)


def _routes_config(app: ReferenceApp, downstream: Downstream) -> None:
    """The public routes' configuration (the decision and resume routes need runtime approvals and checkpoints on)."""
    app.client.app.state.config = worker_config(app, downstream)  # type: ignore[attr-defined]


def _register_tool(app: ReferenceApp) -> None:
    """The tool the operator mapped: a project tool registered through the public API as an external action (never CALIBER's own)."""
    registered = _call(
        app,
        "POST",
        ADMIN,
        "",
        "/tools",
        make_tool_payload(PROTECTED, callable_name="escalate", side_effect_level="external_action"),
    )
    assert registered.status_code in (200, 201), registered.text


def _manifest(workflow_id: str) -> dict[str, Any]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["agent"]["tools"] = [PROTECTED]
    manifest["tools"] = {
        PROTECTED: {
            "registry_ref": f"tool.{PROTECTED}.v1",
            "version_constraint": ">=1.0,<2.0",
            "requires_approval": True,
        }
    }
    return manifest


def admit_run(
    app: ReferenceApp,
    downstream: Downstream,
    *,
    manifest: Callable[[str], dict[str, Any]] = _manifest,
    key: str = "egi-b05",
) -> str:
    """Create, publish and admit a run through the public API as the project editor; returns its run id."""
    _routes_config(app, downstream)
    _register_tool(app)
    created = _call(app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": "EGI governed"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": manifest(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["data"]["version_id"]
    published = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    started = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "please ask for the pay statement",
            "idempotency_key": key,
            **seed_release(app, PROJECT_A),
        },
    )
    assert started.status_code == 202, started.text
    run_id: str = started.json()["data"]["workflow_run_id"]
    return run_id


def proposing_executor() -> FakeWorkflowExecutor:
    """The scripted model: one turn that proposes the protected action (no live model, the same turn every time)."""
    action = ProposedAction(
        connector="document-request",
        connector_version="1",
        effect_class="irreversible_write",
        resource=f"cases/{CASE}/document-requests",
        args={"document_type": DOCUMENT},
    )
    return FakeWorkflowExecutor(
        step_script=[ScriptedStep("proposed_action", (action,), usage=StepUsage(10, 5, 15, 1, 0))]
    )


class Owner:
    """The execution owner: holds the lease in the database and is what a worker's lease provider reads."""

    def __init__(self, app: ReferenceApp, owner_id: str = OWNER) -> None:
        self.app = app
        self.owner_id = owner_id
        self.lease: Lease | None = None

    def take(self) -> Lease:
        with self.app.session_factory() as session:
            lease = acquire(session, self.owner_id, ttl_seconds=LEASE_SECONDS)
            session.commit()
        assert lease is not None, "the lease is held by another owner"
        self.lease = lease
        return lease

    def give_up(self) -> None:
        assert self.lease is not None
        with self.app.session_factory() as session:
            release(session, self.lease)
            session.commit()
        self.lease = None

    def provider(self) -> Lease | None:
        return self.lease


def build_worker(
    app: ReferenceApp,
    downstream: Downstream,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lease_provider: Callable[[], Lease | None] | None,
    executor: FakeWorkflowExecutor | None = None,
    **config_changes: Any,
) -> WorkflowRunWorker:
    monkeypatch.setattr(
        worker_module, "build_executor", lambda *_a, **_k: executor or proposing_executor()
    )
    return WorkflowRunWorker(
        app.session_factory,
        config=worker_config(app, downstream, **config_changes),
        lease_provider=lease_provider,
    )


def run_row(app: ReferenceApp, run_id: str) -> CaliberWorkflowRun:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        session.expunge(run)
        return run


def proposals(app: ReferenceApp) -> list[CaliberEffectProposal]:
    with app.session_factory() as session:
        rows = list(session.execute(select(CaliberEffectProposal)).scalars())
        session.expunge_all()
        return rows


def bound_requests(app: ReferenceApp) -> list[CaliberRuntimeApprovalRequest]:
    with app.session_factory() as session:
        rows = list(
            session.execute(
                select(CaliberRuntimeApprovalRequest).where(
                    CaliberRuntimeApprovalRequest.proposal_id.is_not(None)
                )
            ).scalars()
        )
        session.expunge_all()
        return rows


def decide(app: ReferenceApp, run_id: str, action: str, request_id: str) -> Any:
    return _call(
        app,
        "POST",
        APPROVER_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/approval/{action}",
        {"runtime_approval_id": request_id, "reason": "looks right"},
    )


def resume(app: ReferenceApp, run_id: str) -> Any:
    return _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}/resume", {})


@pytest.fixture
def world(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> Iterator[tuple[ReferenceApp, Downstream]]:
    """The reference app where ``approver_a`` may approve in project A, with the operator's tool registered."""
    _reference_only(reference_app)
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(CaliberProjectMember.user_id == APPROVER_A)
        ).scalar_one()
        member.role = "reviewer"
        session.commit()
    # After the app's own egress policy is bound: the downstream fixture is on loopback.
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    try:
        yield reference_app, downstream
    finally:
        bind_egress_policy(None)


def park(
    app: ReferenceApp,
    downstream: Downstream,
    monkeypatch: pytest.MonkeyPatch,
    owner: Owner,
) -> tuple[str, str, WorkflowRunWorker]:
    """Admit a run and let the worker run it to its park: returns the run id, the approval request id and the worker."""
    run_id = admit_run(app, downstream)
    worker = build_worker(app, downstream, monkeypatch, lease_provider=owner.provider)
    worker._tick()
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("waiting_approval", "waiting_approval"), (
        run.error_code,
        run.error_summary,
    )
    (request,) = bound_requests(app)
    return run_id, request.runtime_approval_id, worker


def test_a_proposal_is_approved_over_http_resumed_and_sent_exactly_once(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = park(app, downstream, monkeypatch, owner)

    # The model only proposed: one recorded effect, one question, nothing at the downstream.
    (proposal,) = proposals(app)
    assert proposal.state == "proposed" and proposal.node_id == NODE
    assert (
        proposal.connector == "document-request" and proposal.effect_class == "irreversible_write"
    )
    (request,) = bound_requests(app)
    assert (request.status, request.proposal_id) == ("pending", proposal.proposal_id)
    assert request.requested_by == EDITOR_A, (
        "the requester is the run's initiator, from the run row"
    )
    assert downstream.mutations() == 0

    # The initiator cannot approve their own run; a different approver can, and the decision does not send anything.
    own = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/approval/approve",
        {"runtime_approval_id": request_id},
    )
    assert own.status_code == 403, own.text
    approved = decide(app, run_id, "approve", request_id)
    assert approved.status_code == 200, approved.text
    assert run_row(app, run_id).status == "waiting_approval", "a decision does not wake the run"
    assert downstream.mutations() == 0
    (decided,) = bound_requests(app)
    assert (decided.status, decided.decided_by, decided.consumed_at) == (
        "approved",
        APPROVER_A,
        None,
    )

    # The worker does not dispatch on a decision alone; an operator resumes the run over HTTP.
    worker._tick()
    assert run_row(app, run_id).status == "waiting_approval"
    assert downstream.mutations() == 0
    resumed = resume(app, run_id)
    assert resumed.status_code == 202, resumed.text
    assert run_row(app, run_id).status == "queued"

    worker._tick()

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("completed", None), (
        run.error_code,
        run.error_summary,
        [(p.state, p.reason) for p in proposals(app)],
    )
    assert downstream.mutations() == 1, "the effect was sent exactly once"
    (done,) = proposals(app)
    assert done.state == "committed" and done.receipt_id is not None
    with app.session_factory() as session:
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        assert receipt.proposal_id == done.proposal_id
        (consumed,) = session.execute(
            select(CaliberRuntimeApprovalRequest).where(
                CaliberRuntimeApprovalRequest.proposal_id.is_not(None)
            )
        ).scalars()
        assert consumed.consumed_at is not None, "the dispatcher consumed the approval once"
    stored = downstream.query(done.logical_key)
    assert stored is not None, "the downstream holds the request under the proposal's stable key"

    # Nothing repeats it: another pass claims nothing, and the spent approval cannot send it again.
    worker._tick()
    assert downstream.mutations() == 1
    again = resume(app, run_id)
    assert again.status_code == 409, again.text
    assert downstream.mutations() == 1


def test_an_ambiguous_answer_parks_the_run_and_the_reconciler_settles_it_without_a_second_send(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The downstream committed and its answer was dropped: the outcome is unknown, so the run waits and nothing is re-sent."""
    app, downstream = world
    owner = Owner(app)
    lease = owner.take()
    run_id, request_id, worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    downstream.set_fault("drop_response_after_commit")

    worker._tick()

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("waiting_effect", "effect_reconciliation")
    assert downstream.mutations() == 1, "the downstream did commit; the platform could not tell"
    (proposal,) = proposals(app)
    assert proposal.state == "unknown", "an answer nobody can read back is not a commit"

    results = reconcile_pass(
        app.session_factory,
        lease=lease,
        targets=targets_from_bindings(parse_bindings(_bindings(downstream))),
        lookup=ask_downstream,
    )
    assert [item.outcome for item in results] == [RECONCILED_COMMITTED]
    assert run_row(app, run_id).status == "queued"
    worker._tick()
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("completed", None), (run.error_code, run.error_summary)
    assert downstream.mutations() == 1, "asked by key, never sent again"
    assert proposals(app)[0].state == "committed"


def test_an_approver_whose_role_was_revoked_after_deciding_sends_nothing(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    with app.session_factory() as session:
        session.execute(
            update(CaliberProjectMember)
            .where(CaliberProjectMember.user_id == APPROVER_A)
            .values(role="viewer")
        )
        session.commit()
    assert resume(app, run_id).status_code == 202

    worker._tick()

    run = run_row(app, run_id)
    assert run.status == "failed"
    assert "dispatch refused: role_revoked" in str(run.error_summary)
    assert downstream.mutations() == 0
    (proposal,) = proposals(app)
    assert proposal.state == "proposed", "refused before the claim: the proposal never moved"
    (request,) = bound_requests(app)
    assert request.consumed_at is None, "and the approval was not spent"


def test_an_approver_scope_revoked_from_the_configured_lists_sends_nothing(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scopes a dispatch attests are derived from the server's lists now, not from what the decision request carried."""
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, _worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    later = build_worker(
        app, downstream, monkeypatch, lease_provider=owner.provider, approver_users=""
    )

    later._tick()

    run = run_row(app, run_id)
    assert run.status == "failed" and "dispatch refused: role_revoked" in str(run.error_summary)
    assert downstream.mutations() == 0


def test_a_worker_that_lost_the_lease_sends_nothing(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    owner.give_up()

    worker._tick()

    run = run_row(app, run_id)
    assert run.status == "failed"
    assert "dispatch refused: lease_unavailable" in str(run.error_summary)
    assert downstream.mutations() == 0
    assert proposals(app)[0].state == "proposed"
    (request,) = bound_requests(app)
    assert request.consumed_at is None


def test_a_worker_with_a_lease_held_by_another_owner_sends_nothing(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that still believes it owns the lease but whose token is not the row's is fenced out by the claim."""
    app, downstream = world
    owner = Owner(app)
    stale = owner.take()
    run_id, request_id, worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    owner.give_up()
    successor = Owner(app, "egi-owner-2")
    successor.take()
    owner.lease = stale  # the old process still holds its captured lease

    worker._tick()

    run = run_row(app, run_id)
    assert run.status == "failed" and "dispatch refused: lease_lost" in str(run.error_summary)
    assert downstream.mutations() == 0


def test_a_worker_never_wired_to_the_lease_does_not_even_ask_the_model(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    run_id = admit_run(app, downstream)
    executor = proposing_executor()
    worker = build_worker(app, downstream, monkeypatch, lease_provider=None, executor=executor)

    worker._tick()

    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("failed", "governed_context_unavailable")
    assert "lease_provider_unbound" in str(run.error_summary)
    assert executor.step_requests == [], "an agent that would propose never runs ungoverned"
    assert proposals(app) == [] and downstream.mutations() == 0


def test_a_mapped_tool_with_no_connector_binding_is_refused_before_the_claim(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, _worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    unbound = build_worker(
        app,
        downstream,
        monkeypatch,
        lease_provider=owner.provider,
        effect_callback_connectors="[]",
    )

    unbound._tick()

    run = run_row(app, run_id)
    assert run.status == "failed" and "dispatch refused: connector_unbound" in str(
        run.error_summary
    )
    assert downstream.mutations() == 0
    assert proposals(app)[0].state == "proposed"


def test_the_development_profile_runs_an_agent_as_before_and_binds_nothing(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The governed path is the reference profile's: with the same trusted maps, a development worker runs the legacy agent."""
    app, downstream = world
    run_id = admit_run(app, downstream)
    executor = FakeWorkflowExecutor()
    worker = build_worker(
        app,
        downstream,
        monkeypatch,
        lease_provider=None,
        executor=executor,
        profile="development",
    )

    worker._tick()

    run = run_row(app, run_id)
    assert run.status == "completed", (run.error_code, run.error_summary)
    assert proposals(app) == [] and bound_requests(app) == []
    assert downstream.mutations() == 0


def test_a_rejected_proposal_is_final_and_sends_nothing(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, worker = park(app, downstream, monkeypatch, owner)
    rejected = decide(app, run_id, "reject", request_id)
    assert rejected.status_code == 200, rejected.text
    assert resume(app, run_id).status_code == 409, "a rejection cannot be resumed past"

    worker._tick()

    assert downstream.mutations() == 0
    assert proposals(app)[0].state == "proposed"


# ----------------------------------------------------------------------- a worker killed mid-dispatch


def _child_env(app: ReferenceApp, downstream: Downstream, barrier: str) -> dict[str, str]:
    return {
        **app.env,
        "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED": "true",
        "CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED": "true",
        "CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED": "true",
        "CALIBER_ADMISSION_ENABLED": "true",
        "CALIBER_AGENT_STEP_TOOL_BACKENDS": _backends(),
        "CALIBER_EFFECT_CALLBACK_CONNECTORS": _bindings(downstream),
        "CALIBER_FAULT_BARRIERS": barrier,
        "PYTHONPATH": str(CALIBER_DIR / "src") + ":" + str(CALIBER_DIR),
    }


def _run_child(
    app: ReferenceApp, downstream: Downstream, barrier: str, owner_id: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "tests.egi_scenarios.harness.worker_child", "--owner-id", owner_id],
        cwd=CALIBER_DIR,
        env=_child_env(app, downstream, barrier),
        capture_output=True,
        timeout=120,
        check=False,
    )


def _expire_run_lease(app: ReferenceApp, run_id: str) -> None:
    """What the janitor of the dead worker's lease would do: let the next worker recover the run it was holding."""
    with app.session_factory() as session:
        session.execute(
            update(CaliberWorkflowRun)
            .where(CaliberWorkflowRun.workflow_run_id == run_id)
            .values(lease_expires_at=None, status="queued", claimed_by=None)
        )
        session.commit()


@pytest.mark.parametrize(
    ("barrier", "downstream_saw_it"),
    [("before_dispatch", False), ("after_downstream_commit", True)],
)
def test_a_worker_killed_mid_dispatch_never_sends_a_second_time(
    world: tuple[ReferenceApp, Downstream],
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
    downstream_saw_it: bool,
) -> None:
    app, downstream = world
    owner = Owner(app)
    owner.take()
    run_id, request_id, _worker = park(app, downstream, monkeypatch, owner)
    assert decide(app, run_id, "approve", request_id).status_code == 200
    assert resume(app, run_id).status_code == 202
    owner.give_up()

    # The execution owner is a separate process; it takes the lease, claims the dispatch and is killed at the barrier.
    killed = _run_child(app, downstream, barrier, "egi-child-owner")
    assert killed.returncode == 137, killed.stderr.decode()[-2000:]
    assert downstream.mutations() == (1 if downstream_saw_it else 0)
    (proposal,) = proposals(app)
    assert proposal.state == "dispatched", "the claim committed before the process died"
    (request,) = bound_requests(app)
    assert request.consumed_at is not None, "and the approval was spent by it, once"

    # A new owner takes the lease. The run is recovered, finds an effect nobody can vouch for and waits for the reconciler.
    with app.session_factory() as session:
        old = current_lease(session)
        assert old is not None
        release(session, old)
        session.commit()
    new_owner = Owner(app, "egi-owner-3")
    lease = new_owner.take()
    _expire_run_lease(app, run_id)
    worker = build_worker(app, downstream, monkeypatch, lease_provider=new_owner.provider)
    worker._tick()
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("waiting_effect", "effect_reconciliation")
    assert downstream.mutations() == (1 if downstream_saw_it else 0), "the recovery sent nothing"

    # The owner's reconciler asks the downstream by key and never sends the request again.
    results = reconcile_pass(
        app.session_factory,
        lease=lease,
        targets=targets_from_bindings(parse_bindings(_bindings(downstream))),
        lookup=ask_downstream,
    )
    if downstream_saw_it:
        assert [item.outcome for item in results] == [RECONCILED_COMMITTED]
        assert run_row(app, run_id).status == "queued", "settling it woke the parked run"
        worker._tick()
        run = run_row(app, run_id)
        assert (run.status, run.error_code) == ("completed", None), (
            run.error_code,
            run.error_summary,
        )
        assert proposals(app)[0].state == "committed"
    else:
        # Killed before the send: the downstream never saw it and a missing answer is not proof of absence, so the effect
        # stays unknown, the run stays parked, and it is never sent behind the platform's back.
        assert [item.outcome for item in results] == [STILL_UNKNOWN]
        assert run_row(app, run_id).status == "waiting_effect"
    assert downstream.mutations() == (1 if downstream_saw_it else 0)
