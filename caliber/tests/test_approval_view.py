"""``GET /workflow-runs/{run_id}/approvals/{approval_id}``: the approver's view of one request (B06.5, #743).

The console's approval panel reads this record and nothing else, so the checks are about what it must and must not say:
the pins and the effect for someone who may decide, the state in the order the decision route judges it, what the caller
may do (and that the routes agree), and that no argument value, hash-bearing payload or foreign record ever leaves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

import caliber.routes.workflow_runs as workflow_runs_routes
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberProject,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER
from caliber.schemas import WorkflowApprovalViewSchema
from caliber.workflows.approval_view import (
    MAX_ARG_FIELDS,
    approvers_of,
    args_shape,
    load_proposal_view,
)
from tests.test_routes_workflow_runs_async import (
    _approval_row,
    _enable_runtime_approvals,
    _seed_bound_approval,
    _seed_waiting_approval,
)
from tests.workflow_helpers import PREFIX, create_and_publish

SECRET = "sk-live-SECRET-VALUE-0123456789"


def _parked(client: TestClient, **seed: Any) -> tuple[str, str, str]:
    """A run waiting at ``human_gate`` with a live bound request: ``(run_id, request_id, proposal_id)``."""
    _enable_runtime_approvals(client)
    wid, vid = create_and_publish(client)
    with client.app.state.session_factory() as session:
        workflow = session.get(CaliberWorkflow, wid)
        assert workflow is not None
        workflow.visibility = (
            "public"  # any signed-in user can read the run: these tests vary who may *decide*
        )
        session.commit()
    created = client.post(
        f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid, "input": "hello"}
    )
    run_id = created.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(
        client, run_id, approval_status="approved", checkpoint_input_by_port={"input": "hello"}
    )
    bound_id, proposal_id = _seed_bound_approval(client, run_id, **seed)
    return run_id, bound_id, proposal_id


def _view_url(run_id: str, approval_id: str) -> str:
    return f"{PREFIX}/workflow-runs/{run_id}/approvals/{approval_id}"


def _view(client: TestClient, run_id: str, approval_id: str, **kwargs: Any) -> dict[str, Any]:
    response = client.get(_view_url(run_id, approval_id), **kwargs)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def test_a_bound_open_request_shows_the_pins_the_effect_and_the_server_signals(
    client: TestClient,
) -> None:
    run_id, bound_id, proposal_id = _parked(client)
    row = _approval_row(client, bound_id)

    data = _view(client, run_id, bound_id)

    assert WorkflowApprovalViewSchema.model_validate(data)  # the documented shape, nothing extra
    assert data["approval"]["runtime_approval_id"] == bound_id
    assert data["approval"]["status"] == "pending"
    assert data["binding"] == {
        "proposal_id": proposal_id,
        "intent_hash": row.intent_hash,
        "evidence_hash": "sha256:" + "e" * 64,
        "policy_digest": row.policy_digest,
        "expected_resource_version": row.expected_resource_version,
        "expires_at": data["binding"]["expires_at"],
        "requested_by": "editor_a",
        "superseded_by": None,
        "consumed_at": None,
    }
    proposal = data["proposal"]
    assert (proposal["connector"], proposal["connector_version"]) == ("document-request", "1.0.0")
    assert (proposal["effect_class"], proposal["state"]) == ("irreversible_write", "proposed")
    assert proposal["resource"] == "cases/MORT-1/document-requests"
    assert proposal["args_fields"] == [{"name": "document_type", "kind": "string"}]
    assert data["state"] == {"code": "open", "superseded_by": None}
    assert data["quorum"] == {"approvers": [], "required": 1, "remaining": 1}
    assert data["policy"]["required_role"] == "caliber.approver"
    assert data["allowed_responses"] == ["approve", "reject"]
    assert data["caller"] == {
        "principal": "@test",
        "approve": {"allowed": True, "reason_code": None},
        "reject": {"allowed": True, "reason_code": None},
    }


def test_no_argument_value_leaves_the_server_only_the_names_and_kinds(client: TestClient) -> None:
    """The one place a secret or a personal datum can sit is the proposal's arguments: the view carries their shape only."""
    run_id, bound_id, _proposal_id = _parked(
        client,
        args={
            "api_key": SECRET,
            "customer": {"email": "someone@example.com", "ssn": "078-05-1120"},
            "amounts": [1, 2, 3],
            "dry_run": False,
            "limit": 5,
            "note": None,
        },
    )
    response = client.get(_view_url(run_id, bound_id))
    assert response.status_code == 200, response.text
    for value in (SECRET, "someone@example.com", "078-05-1120"):
        assert value not in response.text
    fields = response.json()["data"]["proposal"]["args_fields"]
    assert fields == [
        {"name": "amounts", "kind": "array"},
        {"name": "api_key", "kind": "string"},
        {"name": "customer", "kind": "object"},
        {"name": "dry_run", "kind": "boolean"},
        {"name": "limit", "kind": "number"},
        {"name": "note", "kind": "null"},
    ]


def test_the_argument_shape_bounds_names_and_count() -> None:
    """A key is attacker-supplied text: only a plain identifier is named, and the list is capped and says so."""
    fields, total, truncated = args_shape(
        {
            "ok_name": 1,
            SECRET + " with spaces": 2,
            "x" * 65: 3,
            "9starts-with-digit": 4,
            "": 5,
        }
    )
    assert total == 5 and truncated is False
    assert sorted((f["name"] or "", f["kind"]) for f in fields) == [("", "number")] * 4 + [
        ("ok_name", "number")
    ]
    assert SECRET not in repr(fields)

    many = {f"k{i:03d}": i for i in range(MAX_ARG_FIELDS + 7)}
    fields, total, truncated = args_shape(many)
    assert (len(fields), total, truncated) == (MAX_ARG_FIELDS, MAX_ARG_FIELDS + 7, True)

    for not_a_mapping in (None, "x", ["a"], 3):
        assert args_shape(not_a_mapping) == ([], 0, False)


def test_a_legacy_node_level_request_has_no_binding_and_no_effect(client: TestClient) -> None:
    _enable_runtime_approvals(client)
    _wid, vid = create_and_publish(client)
    run_id = client.post(
        f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid, "input": "hello"}
    ).json()["data"]["workflow_run_id"]
    legacy_id = _seed_waiting_approval(
        client, run_id, approval_status="pending", checkpoint_input_by_port={"input": "hello"}
    )

    data = _view(client, run_id, legacy_id)

    assert data["binding"] is None and data["proposal"] is None
    assert data["state"]["code"] == "open"
    assert data["caller"]["approve"]["allowed"] is True


def _states(client: TestClient, run_id: str, bound_id: str) -> str:
    return str(_view(client, run_id, bound_id)["state"]["code"])


def _mutate(client: TestClient, bound_id: str, **columns: Any) -> None:
    with client.app.state.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, bound_id)
        assert row is not None
        for name, value in columns.items():
            setattr(row, name, value)
        session.commit()


def test_the_state_is_judged_in_the_order_of_the_decision_route(client: TestClient) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    now = datetime.now(timezone.utc)
    assert _states(client, run_id, bound_id) == "open"

    _mutate(client, bound_id, expires_at=now - timedelta(seconds=1))
    assert _states(client, run_id, bound_id) == "expired"

    _mutate(client, bound_id, superseded_by="RAP-newer")
    view = _view(client, run_id, bound_id)
    assert view["state"] == {"code": "superseded", "superseded_by": "RAP-newer"}, (
        "superseded is reported before expired, and names the newer request"
    )

    _mutate(
        client,
        bound_id,
        consumed_at=now,
        status="approved",
        superseded_by=None,
        expires_at=now - timedelta(seconds=1),
    )
    assert _states(client, run_id, bound_id) == "already_consumed", (
        "a consumed request stays terminal after its expiry passes"
    )


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        ({"status": "approved"}, "already_decided"),
        ({"status": "rejected"}, "rejected"),
        ({"status": "superseded"}, "superseded"),
        ({"node_id": "another_gate"}, "approval_not_current"),
    ],
)
def test_each_final_state_is_named(
    client: TestClient, columns: dict[str, Any], expected: str
) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    _mutate(client, bound_id, **columns)
    assert _states(client, run_id, bound_id) == expected


def test_a_run_that_is_no_longer_waiting_is_named_and_decidable_by_nobody(
    client: TestClient,
) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "cancelled"
        session.commit()
    data = _view(client, run_id, bound_id)
    assert data["state"]["code"] == "run_not_waiting"
    assert data["caller"]["approve"] == {"allowed": False, "reason_code": "run_not_waiting"}
    assert data["caller"]["reject"] == {"allowed": False, "reason_code": "run_not_waiting"}


def test_the_clock_is_the_routes_clock(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The view judges expiry by the decision route's own seam, so a test (and the route) never use two clocks."""
    run_id, bound_id, _proposal_id = _parked(client)
    expires = _approval_row(client, bound_id).expires_at
    assert expires is not None
    monkeypatch.setattr(
        workflow_runs_routes,
        "_utc_now",
        lambda: expires.replace(tzinfo=timezone.utc),
    )
    assert _states(client, run_id, bound_id) == "expired", (
        "half-open window: at expires_at it is refused"
    )
    monkeypatch.setattr(
        workflow_runs_routes,
        "_utc_now",
        lambda: expires.replace(tzinfo=timezone.utc) - timedelta(microseconds=1),
    )
    assert _states(client, run_id, bound_id) == "open"


def test_the_requester_cannot_approve_but_may_reject_and_the_route_agrees(
    client: TestClient,
) -> None:
    run_id, bound_id, _proposal_id = _parked(client, requested_by="@test", allow_self=False)

    data = _view(client, run_id, bound_id)

    assert data["caller"]["approve"] == {"allowed": False, "reason_code": "self_approval"}
    assert data["caller"]["reject"] == {"allowed": True, "reason_code": None}
    refused = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/approve", json={"runtime_approval_id": bound_id}
    )
    assert refused.status_code == 403, refused.text


def test_the_run_initiator_cannot_approve_and_the_route_agrees(client: TestClient) -> None:
    """The route also compares the caller with the run's creator (``record_approval``), not only the request's requester."""
    run_id, bound_id, _proposal_id = _parked(client, allow_self=False)
    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.created_by = "@test"
        session.commit()
    data = _view(client, run_id, bound_id)
    assert data["caller"]["approve"] == {"allowed": False, "reason_code": "self_approval"}
    assert (
        client.post(
            f"{PREFIX}/workflow-runs/{run_id}/approval/approve",
            json={"runtime_approval_id": bound_id},
        ).status_code
        == 403
    )


def test_a_quorum_lists_who_approved_and_blocks_the_same_person_twice(client: TestClient) -> None:
    run_id, bound_id, _proposal_id = _parked(client, approval_count=2)
    approve = f"{PREFIX}/workflow-runs/{run_id}/approval/approve"
    assert client.post(approve, json={"runtime_approval_id": bound_id}).status_code == 200

    data = _view(client, run_id, bound_id)

    assert data["state"]["code"] == "open", "one of two: still pending"
    assert data["quorum"] == {"approvers": ["@test"], "required": 2, "remaining": 1}
    assert data["caller"]["approve"] == {"allowed": False, "reason_code": "duplicate_approver"}
    assert data["caller"]["reject"]["allowed"] is True
    assert client.post(approve, json={"runtime_approval_id": bound_id}).status_code == 403
    other = _view(client, run_id, bound_id, headers={"X-CALIBER-User": "@reza"})
    assert other["caller"]["approve"] == {"allowed": True, "reason_code": None}
    assert other["quorum"]["approvers"] == ["@test"]


def test_a_caller_without_the_nodes_scope_is_told_which_scope_is_missing(
    client: TestClient,
) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    data = _view(client, run_id, bound_id, headers={"X-CALIBER-User": "@viewer"})
    assert data["caller"]["principal"] == "@viewer"
    assert data["caller"]["approve"] == {"allowed": False, "reason_code": "insufficient_scope"}
    assert data["caller"]["reject"] == {"allowed": False, "reason_code": "insufficient_scope"}
    refused = client.post(
        f"{PREFIX}/workflow-runs/{run_id}/approval/reject",
        json={"runtime_approval_id": bound_id},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert refused.status_code == 403, refused.text


def _project_run(client: TestClient, run_id: str, bound_id: str, members: dict[str, str]) -> None:
    with client.app.state.session_factory() as session:
        session.add(CaliberProject(project_id="PRJ-view", name="Approval view", owner="@owner"))
        session.flush()
        for index, (user, role) in enumerate(members.items()):
            session.add(
                CaliberProjectMember(
                    member_id=f"M-view-{index}",
                    project_id="PRJ-view",
                    user_id=user,
                    role=role,
                    created_by="@owner",
                )
            )
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.project_id = "PRJ-view"
        session.commit()


def test_a_project_member_without_the_approve_action_sees_the_pins_but_not_the_effect(
    client: TestClient,
) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    _project_run(
        client,
        run_id,
        bound_id,
        {"@reza": ROLE_REVIEWER, "@sarah": ROLE_VIEWER, "@alex": ROLE_EDITOR},
    )

    reviewer = _view(client, run_id, bound_id, headers={"X-CALIBER-User": "@reza"})
    assert reviewer["proposal"] is not None
    assert reviewer["caller"]["approve"]["allowed"] is True

    for user in ("@sarah", "@alex"):  # a viewer and an editor hold no approve action
        data = _view(client, run_id, bound_id, headers={"X-CALIBER-User": user})
        assert data["proposal"] is None, (
            "what the effect would do is shown to those who may decide it"
        )
        assert data["binding"]["requested_by"] is None, "so is who asked for it"
        assert data["binding"]["intent_hash"] == reviewer["binding"]["intent_hash"]
        assert data["caller"]["approve"] == {
            "allowed": False,
            "reason_code": "project_role_required",
        }
        assert data["caller"]["reject"] == {
            "allowed": False,
            "reason_code": "project_role_required",
        }
        refused = client.post(
            f"{PREFIX}/workflow-runs/{run_id}/approval/reject",
            json={"runtime_approval_id": bound_id},
            headers={"X-CALIBER-User": user},
        )
        assert refused.status_code == 403, refused.text


def test_a_non_member_gets_the_same_404_as_an_unknown_approval(client: TestClient) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    _project_run(client, run_id, bound_id, {"@reza": ROLE_REVIEWER})

    outsider = client.get(_view_url(run_id, bound_id), headers={"X-CALIBER-User": "@a"})
    unknown = client.get(_view_url(run_id, "RAP-none"), headers={"X-CALIBER-User": "@reza"})
    assert outsider.status_code == 404
    assert unknown.status_code == 404
    assert outsider.json()["detail"] == f"workflow run {run_id!r} not found"
    assert bound_id not in outsider.text


def test_an_approval_of_another_run_is_not_found_under_this_run(client: TestClient) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    _wid, vid = create_and_publish(client, workflow_name="Other Workflow")
    other_run = client.post(
        f"{PREFIX}/workflow-runs", json={"workflow_version_id": vid, "input": "hello"}
    ).json()["data"]["workflow_run_id"]
    response = client.get(_view_url(other_run, bound_id))
    assert response.status_code == 404, response.text
    assert "RAP-" not in response.text.replace(repr(bound_id), "")
    assert client.get(_view_url("WR-missing", bound_id)).status_code == 404
    assert client.get(_view_url(run_id, bound_id)).status_code == 200


def test_an_unauthenticated_caller_is_refused(client: TestClient) -> None:
    run_id, bound_id, _proposal_id = _parked(client)
    response = client.get(_view_url(run_id, bound_id), headers={"X-CALIBER-User": ""})
    assert response.status_code == 401


def test_the_view_never_loads_the_intent_bytes_or_writes(client: TestClient) -> None:
    """A read: rows and the audit trail are untouched, and the stored intent is not in the response."""
    run_id, bound_id, proposal_id = _parked(client)
    before = _approval_row(client, bound_id)
    response = client.get(_view_url(run_id, bound_id))
    after = _approval_row(client, bound_id)
    assert (after.status, after.decided_by, after.policy_snapshot, after.consumed_at) == (
        before.status,
        before.decided_by,
        before.policy_snapshot,
        before.consumed_at,
    )
    with client.app.state.session_factory() as session:
        proposal = session.execute(
            select(CaliberEffectProposal).where(CaliberEffectProposal.proposal_id == proposal_id)
        ).scalar_one()
        assert proposal.state == "proposed"
        assert proposal.intent_bytes.decode("utf-8", "replace") not in response.text


def test_the_list_route_marks_bound_requests_so_the_console_opens_the_panel(
    client: TestClient,
) -> None:
    run_id, bound_id, proposal_id = _parked(client)
    rows = client.get(f"{PREFIX}/workflow-runs/{run_id}/approvals").json()["data"]
    by_id = {row["runtime_approval_id"]: row for row in rows}
    bound = by_id[bound_id]
    assert bound["proposal_id"] == proposal_id
    assert bound["expires_at"] is not None
    assert bound["superseded_by"] is None and bound["consumed_at"] is None
    legacy = next(row for rid, row in by_id.items() if rid != bound_id)
    assert legacy["proposal_id"] is None and legacy["expires_at"] is None


def test_a_proposal_of_another_run_is_never_read_for_this_requests_view(client: TestClient) -> None:
    """The proposal is read by id *and* the request's own run, so a request can never show a foreign run's effect."""
    run_id, bound_id, proposal_id = _parked(client)
    with client.app.state.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, bound_id)
        assert row is not None
        assert load_proposal_view(session, row) is not None
        foreign = SimpleNamespace(proposal_id=proposal_id, workflow_run_id="WR-another")
        assert load_proposal_view(session, foreign) is None  # type: ignore[arg-type]
        unbound = SimpleNamespace(proposal_id=None, workflow_run_id=run_id)
        assert load_proposal_view(session, unbound) is None  # type: ignore[arg-type]


def test_the_approvers_are_distinct_bounded_and_read_defensively() -> None:
    def snapshot(value: object) -> SimpleNamespace:
        return SimpleNamespace(policy_snapshot={"approvals": value})

    assert approvers_of(SimpleNamespace(policy_snapshot=None)) == []  # type: ignore[arg-type]
    assert approvers_of(SimpleNamespace(policy_snapshot={})) == []  # type: ignore[arg-type]
    assert approvers_of(snapshot("not a list")) == []  # type: ignore[arg-type]
    assert approvers_of(snapshot(["a", "b", "a", "", 7, None, "c"])) == ["a", "b", "c"]  # type: ignore[arg-type]
    assert approvers_of(snapshot(["x" * 400])) == ["x" * 256]  # type: ignore[arg-type]
    many = [f"user-{index}" for index in range(250)]
    assert approvers_of(snapshot(many)) == many[:100]  # type: ignore[arg-type]
