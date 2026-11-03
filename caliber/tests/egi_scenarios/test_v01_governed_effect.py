"""V01 Beta acceptance path: a governed evidence-review effect is accepted end to end.

The scenario deliberately composes the existing B05 public worker seams and the
pinned Harborlight downstream fixture.  An editor admits the run and the
scripted model proposes one document request.  The editor cannot approve the
effect; the distinct approver in project A approves it, the operator
resumes the run, and the real worker sends one request through the governed
dispatcher.  The downstream receipt, acceptance decision, causal-links page,
and project-scoped public read are all checked against the same persisted ids.

Project B is present in the shared fixture to prove the read/approval boundary:
its editor cannot read project A's run or its causal links, and cannot cause a
second downstream mutation.
"""
# ruff: noqa: F811 - the imported B05 fixture is intentionally reused by name

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

from caliber.contracts.agent_step import AgentStepRequest, ProposedAction
from caliber.db.models import CaliberEffectProposal, CaliberEffectReceipt
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.workflows.runtime import FakeWorkflowExecutor
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    PROXY_SECRET,
    Downstream,
    ReferenceApp,
    ScriptedModel,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_b05_governed_worker_wiring import (
    CASE,
    DOCUMENT,
    Owner,
    admit_run,
    bound_requests,
    build_worker,
    decide,
    resume,
    run_row,
    world,  # noqa: F401 - imported pytest fixture
)

pytestmark = pytest.mark.xdist_group("egi-scenario-processes")

PROJECT_ROLE_DETAIL = "project role 'editor' cannot perform resource.approve"


def _scripted_executor(model: ScriptedModel) -> FakeWorkflowExecutor:
    """Adapt the shared two-turn model script to the governed-step contract."""

    def answer(request: AgentStepRequest) -> Mapping[str, Any]:
        turn = model.next_turn()
        if turn["type"] == "tool_call":
            assert turn["name"] == "request_missing_document"
            arguments = turn["arguments"]
            action = ProposedAction(
                connector="document-request",
                connector_version="1",
                effect_class="irreversible_write",
                resource=f"cases/{arguments['case_id']}/document-requests",
                args={"document_type": arguments["document_type"]},
                expected_resource_version="1",
            )
            stop_reason = "proposed_action"
            proposed_actions = [action.record()]
            usage = {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "turns": 1,
                "tool_calls": 1,
            }
        else:
            assert turn["type"] == "completed"
            stop_reason = "completed"
            proposed_actions = []
            usage = {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
                "turns": 1,
                "tool_calls": 0,
            }
        return {
            "schema_version": "v1",
            "step_request_id": request.step_request_id,
            "workflow_run_id": request.workflow_run_id,
            "node_id": request.node_id,
            "occurrence_id": request.occurrence_id,
            "attempt": request.attempt,
            "stop_reason": stop_reason,
            "proposed_actions": proposed_actions,
            "usage": usage,
        }

    # A resume may replay the agent step, so retain both scripted turns.  The
    # runtime normally resumes from the persisted approval checkpoint and uses
    # only the first entry; either behavior remains deterministic and bounded.
    return FakeWorkflowExecutor(step_script=[answer, answer])


@dataclass
class V01Run:
    app: ReferenceApp
    downstream: Downstream
    owner: Owner
    worker: WorkflowRunWorker
    run_id: str
    approval_id: str


@dataclass(frozen=True)
class EffectEvidence:
    proposal_id: str
    receipt_id: str
    downstream_request_id: str


@pytest.fixture
def completed_v01(
    world: tuple[ReferenceApp, Downstream],
    two_workspaces: Workspaces,
    scripted_model: ScriptedModel,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[V01Run]:
    """Run V01 through the real reference worker and leave the lease for teardown."""

    app, downstream = world
    assert (two_workspaces.project_a, two_workspaces.project_b) == (PROJECT_A, PROJECT_B)
    assert (two_workspaces.editor_a, two_workspaces.approver_a, two_workspaces.editor_b) == (
        EDITOR_A,
        APPROVER_A,
        EDITOR_B,
    )

    owner = Owner(app)
    owner.take()
    run_id = admit_run(app, downstream, key="egi-b15-2-v01")
    worker = build_worker(
        app,
        downstream,
        monkeypatch,
        lease_provider=owner.provider,
        executor=_scripted_executor(scripted_model),
    )

    worker._tick()
    run = run_row(app, run_id)
    assert (run.status, run.error_code) == ("waiting_approval", "waiting_approval"), (
        run.error_code,
        run.error_summary,
    )
    (request,) = bound_requests(app)
    approval_id = request.runtime_approval_id

    # The approval view is the exact UI contract: the initiator is told why the
    # button is disabled, and the decision route returns the same refusal.
    view = _call(
        app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/approvals/{approval_id}",
    )
    assert view.status_code == 200, view.text
    assert view.json()["data"]["caller"]["approve"] == {
        "allowed": False,
        "reason_code": "project_role_required",
    }
    editor_approval = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/approval/approve",
        {"runtime_approval_id": approval_id},
    )
    assert editor_approval.status_code == 403, editor_approval.text
    assert editor_approval.json()["detail"] == PROJECT_ROLE_DETAIL
    assert downstream.mutations() == 0

    approved = decide(app, run_id, "approve", approval_id)
    assert approved.status_code == 200, approved.text
    assert resume(app, run_id).status_code == 202
    worker._tick()

    final = run_row(app, run_id)
    assert (final.status, final.error_code) == ("completed", None), (
        final.error_code,
        final.error_summary,
    )
    assert downstream.mutations() == 1
    assert scripted_model.calls and scripted_model.calls[0]["type"] == "tool_call"

    try:
        yield V01Run(app, downstream, owner, worker, run_id, approval_id)
    finally:
        owner.give_up()


def _assert_effect_and_downstream(completed_v01: V01Run) -> EffectEvidence:
    """Check the one real downstream mutation and its verified persisted receipt."""

    app, downstream = completed_v01.app, completed_v01.downstream
    with app.session_factory() as session:
        proposal = session.execute(select(CaliberEffectProposal)).scalar_one()
        receipt = session.execute(select(CaliberEffectReceipt)).scalar_one()
        assert len(list(session.execute(select(CaliberEffectProposal)).scalars())) == 1
        assert len(list(session.execute(select(CaliberEffectReceipt)).scalars())) == 1
        assert proposal.workflow_run_id == completed_v01.run_id
        assert proposal.state == "committed"
        assert proposal.connector == "document-request"
        assert proposal.connector_version == "1"
        assert proposal.resource == f"cases/{CASE}/document-requests"
        assert proposal.args == {"document_type": DOCUMENT}
        assert proposal.expected_resource_version == "1"
        assert proposal.receipt_id == receipt.receipt_id
        assert receipt.proposal_id == proposal.proposal_id
        assert receipt.resource == proposal.resource
        assert receipt.resource_version == "2"
        assert receipt.verified_by == "query"
        evidence = EffectEvidence(
            proposal_id=proposal.proposal_id,
            receipt_id=receipt.receipt_id,
            downstream_request_id=receipt.downstream_request_id,
        )

    case = downstream.client.get(f"/cases/{CASE}")
    assert case.status_code == 200, case.text
    assert case.json()["version"] == 2
    assert case.json()["document_requests"] == [
        {
            "request_id": evidence.downstream_request_id,
            "document_type": DOCUMENT,
            "case_version": 2,
        }
    ]
    assert downstream.mutations() == 1
    return evidence


def _assert_public_acceptance(completed_v01: V01Run, proposal_id: str) -> dict[str, Any]:
    """Check the run's public terminal and acceptance contract."""

    public = _call(
        completed_v01.app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{completed_v01.run_id}",
    )
    assert public.status_code == 200, public.text
    public_data = public.json()["data"]
    assert isinstance(public_data, dict)
    assert public_data["workflow_run_id"] == completed_v01.run_id
    assert public_data["status"] == "completed"
    assert public_data["acceptance"]["status"] == "accepted"
    assert len(public_data["acceptance"]["decisions"]) == 1
    assert public_data["acceptance"]["decisions"][0]["candidate_id"] == proposal_id
    assert public_data["acceptance"]["decisions"][0]["outcome"] == "accepted"
    return dict(public_data)


def _assert_causal_links(completed_v01: V01Run, evidence: EffectEvidence) -> None:
    """Check that every governed-effect row is linked and scoped to this run."""

    app = completed_v01.app
    links_response = _call(
        app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{completed_v01.run_id}/causal-links",
    )
    assert links_response.status_code == 200, links_response.text
    links = links_response.json()["data"]
    assert links["workflow_run_id"] == completed_v01.run_id
    assert links["project_id"] == PROJECT_A
    assert links["section"] == "effects"
    assert links["has_more"] is False
    assert links["next_cursor"] is None
    assert links["run_manifest"]["status"] == "linked"
    (effect,) = links["effects"]
    assert effect["proposal_id"] == evidence.proposal_id
    assert effect["state"] == "committed"
    assert effect["context_manifest"]["status"] == "linked"
    assert effect["approval"]["status"] == "linked"
    assert effect["approval"]["approval_id"] == completed_v01.approval_id
    assert effect["approval"]["request_status"] == "approved"
    assert effect["approval"]["consumed_at"] is not None
    assert effect["decision"]["status"] == "linked"
    assert effect["decision"]["result"] == "allow"
    assert effect["effect"]["status"] == "linked"
    assert effect["effect"]["effect_id"] == evidence.receipt_id
    assert effect["effect"]["committed"] is True
    assert effect["missing_links"] == []
    assert effect["unrecorded_links"] == []
    assert effect["span_attributes"] == {
        "caliber.proposal_id": evidence.proposal_id,
        "caliber.approval_id": completed_v01.approval_id,
        "caliber.decision_id": effect["decision"]["decision_id"],
        "caliber.effect_id": evidence.receipt_id,
    }

    decisions = _call(
        app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{completed_v01.run_id}/causal-links?section=policy_decisions&limit=200",
    )
    assert decisions.status_code == 200, decisions.text
    assert effect["decision"]["decision_id"] in {
        row["decision_id"] for row in decisions.json()["data"]["policy_decisions"]
    }


def test_v01_governed_effect_has_one_verified_accepted_effect_and_complete_causal_chain(
    completed_v01: V01Run,
) -> None:
    """The public V01 oracle matches the independent downstream and every persisted link."""

    evidence = _assert_effect_and_downstream(completed_v01)
    _assert_public_acceptance(completed_v01, evidence.proposal_id)
    _assert_causal_links(completed_v01, evidence)


def test_v01_project_b_cannot_read_project_a_run_or_causal_links(
    completed_v01: V01Run,
) -> None:
    """The second project sees the same not-found contract and cannot affect the one effect."""

    app, downstream = completed_v01.app, completed_v01.downstream
    run = _call(
        app,
        "GET",
        EDITOR_B,
        PROJECT_B,
        f"/workflow-runs/{completed_v01.run_id}",
    )
    causal = _call(
        app,
        "GET",
        EDITOR_B,
        PROJECT_B,
        f"/workflow-runs/{completed_v01.run_id}/causal-links",
    )
    assert (run.status_code, causal.status_code) == (404, 404)
    assert downstream.mutations() == 1


def test_v01_ui_and_sdk_report_the_same_run_and_effect_ids(completed_v01: V01Run) -> None:
    """The installed SDK decodes the same public identifiers the UI GET serves."""

    caliber_sdk = pytest.importorskip("caliber_sdk", reason="caliber-sdk is not installed")
    app = completed_v01.app
    ui = _call(
        app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{completed_v01.run_id}",
    ).json()["data"]
    sdk = caliber_sdk.CaliberClient(
        "http://testserver",
        user=EDITOR_A,
        proxy_secret=PROXY_SECRET,
        project=PROJECT_A,
        http_client=app.client,
    )
    run = sdk.workflows.runs.get(completed_v01.run_id)
    links = sdk.workflows.runs.causal_links(completed_v01.run_id)
    ui_links = _call(
        app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{completed_v01.run_id}/causal-links",
    ).json()["data"]

    assert (run.workflow_run_id, run.project_id, run.status) == (
        ui["workflow_run_id"],
        ui["project_id"],
        ui["status"],
    )
    assert run.acceptance == ui["acceptance"]
    assert links.workflow_run_id == ui["workflow_run_id"]
    assert len(links.effects) == 1
    assert links.effects[0].proposal_id == ui["acceptance"]["decisions"][0]["candidate_id"]
    assert links.effects[0].proposal_id == ui_links["effects"][0]["proposal_id"]
    assert links.effects[0].effect.effect_id == ui_links["effects"][0]["effect"]["effect_id"]
