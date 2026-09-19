"""Phase 1 — Aria goal-plans: planner decomposition, persistence, and routes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.assistant.capabilities import Capability, registered_capabilities
from caliber.assistant.plans import HeuristicPlanner, PlannedStep, PlanService
from caliber.db.models import (
    CaliberAriaPlan,
    CaliberAriaPlanStep,
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
)
from caliber.resource_access import POLICY_VERSION, ROLE_EDITOR, ROLE_VIEWER
from caliber.routes.aria_plans import (
    APPROVE_PATH,
    DETAIL_PATH,
    EXECUTE_PATH,
    INTERACTIONS_PATH,
    LIST_PATH,
    POLL_PATH,
)

# --- planner ----------------------------------------------------------------


def test_heuristic_planner_decomposes_by_capability_domain() -> None:
    planner = HeuristicPlanner()
    caps = registered_capabilities()
    steps = planner.plan("create a faithfulness judge and a review queue", capabilities=caps)
    keys = {s.capability_key for s in steps}
    # Mentions "judge" and "review" → both create capabilities; read caps excluded.
    assert "judge.create" in keys
    assert "review_queue.create" in keys
    assert all(not k.endswith(".list") for k in keys)


def test_heuristic_planner_empty_when_no_domain_match() -> None:
    steps = HeuristicPlanner().plan("write me a poem", capabilities=registered_capabilities())
    assert steps == []


def test_heuristic_planner_wires_declared_step_outputs() -> None:
    steps = HeuristicPlanner().plan(
        "create a review queue and add review items",
        capabilities=registered_capabilities(),
    )
    create_step, add_step = steps
    assert create_step.capability_key == "review_queue.create"
    assert add_step.capability_key == "review_queue.add_items"
    assert add_step.depends_on == [0]
    assert add_step.inputs["queue_id"] == {
        "$from_step_index": 0,
        "path": "queue_id",
    }


def test_planner_is_registry_driven() -> None:
    # A planner sees a freshly-registered capability without code changes.
    def _h(_c, _a):  # pragma: no cover - not invoked here
        return None

    cap = Capability(
        key="widget.create", title="Create widget", description="x", tier="mutate", handler=_h
    )
    caps = [*registered_capabilities(), cap]
    steps = HeuristicPlanner().plan("please create a widget", capabilities=caps)
    assert any(s.capability_key == "widget.create" for s in steps)


# --- service ----------------------------------------------------------------


def test_create_plan_persists_plan_and_steps(session_factory) -> None:
    svc = PlanService()
    detail = svc.create_plan(
        session_factory=session_factory,
        goal="create a judge",
        owner="@reza",
        project_id="PRJ-77",
        constraints={"must_test": True},
        done_when=["judge exists"],
        context_refs=[
            {"ref_type": "workflow", "ref_id": "WF-1", "label": "Support Flow"},
        ],
    )
    assert detail["plan"]["plan_id"].startswith("PLAN-")
    assert detail["plan"]["status"] == "draft"
    assert detail["plan"]["step_count"] >= 1
    assert detail["plan"]["project_id"] == "PRJ-77"
    assert detail["plan"]["constraints"] == {"must_test": True}
    assert detail["plan"]["done_when"] == ["judge exists"]
    assert detail["plan"]["context_refs"][0]["ref_id"] == "WF-1"
    assert detail["steps"][0]["step_id"].startswith("PSTEP-")
    assert detail["steps"][0]["capability_key"] == "judge.create"
    assert detail["steps"][0]["input_schema"]["required"] == ["name", "instructions"]
    # Persisted + audited.
    with session_factory() as db:
        plan = db.get(CaliberAriaPlan, detail["plan"]["plan_id"])
        assert plan is not None and plan.owner == "@reza"
        assert plan.project_id == "PRJ-77"
        assert plan.constraints == {"must_test": True}
        assert plan.done_when == ["judge exists"]
        assert plan.context_refs[0]["ref_id"] == "WF-1"
        n = (
            db.execute(
                select(CaliberAriaPlanStep).where(CaliberAriaPlanStep.plan_id == plan.plan_id)
            )
            .scalars()
            .all()
        )
        assert len(n) == detail["plan"]["step_count"]
        actions = {r.action for r in db.execute(select(CaliberAuditLog)).scalars().all()}
        assert "create_aria_plan" in actions


def test_service_uses_injected_planner_and_links_dependencies(session_factory) -> None:
    class _TwoStep:
        def plan(self, goal, *, capabilities):
            return [
                PlannedStep(capability_key="judge.create", title="A"),
                PlannedStep(capability_key="review_queue.create", title="B", depends_on=[0]),
            ]

    detail = PlanService(planner=_TwoStep()).create_plan(
        session_factory=session_factory, goal="x", owner="@reza"
    )
    steps = detail["steps"]
    assert [s["seq"] for s in steps] == [0, 1]
    # depends_on index 0 resolved to the first step's id.
    assert steps[1]["depends_on"] == [steps[0]["step_id"]]


# --- routes -----------------------------------------------------------------


def test_route_create_get_approve_flow(client: TestClient) -> None:
    created = client.post(LIST_PATH, json={"goal": "create a judge and a review queue"})
    assert created.status_code == 201, created.text
    plan_id = created.json()["data"]["plan"]["plan_id"]
    assert created.json()["data"]["plan"]["step_count"] >= 2

    got = client.get(DETAIL_PATH.replace("{plan_id}", plan_id))
    assert got.status_code == 200
    assert got.json()["data"]["plan"]["plan_id"] == plan_id

    approved = client.post(APPROVE_PATH.replace("{plan_id}", plan_id))
    assert approved.status_code == 200
    assert approved.json()["data"]["plan"]["status"] == "approved"


def test_route_create_plan_persists_task_context_and_project(
    client: TestClient,
    db_session: Session,
) -> None:
    created = client.post(
        LIST_PATH,
        json={
            "goal": "create a judge",
            "constraints": {"must_test": True},
            "done_when": ["judge exists"],
            "context_refs": [{"ref_type": "workflow", "ref_id": "WF-1", "label": "Support Flow"}],
        },
        headers={"X-CALIBER-Project": "PRJ-88"},
    )
    assert created.status_code == 201, created.text
    plan = created.json()["data"]["plan"]
    assert plan["project_id"] == "PRJ-88"
    assert plan["constraints"] == {"must_test": True}
    assert plan["done_when"] == ["judge exists"]
    assert plan["context_refs"][0]["ref_id"] == "WF-1"

    row = db_session.get(CaliberAriaPlan, plan["plan_id"])
    assert row is not None
    assert row.project_id == "PRJ-88"
    assert row.constraints == {"must_test": True}
    assert row.done_when == ["judge exists"]
    assert row.context_refs[0]["label"] == "Support Flow"


def test_route_approve_twice_conflicts(client: TestClient) -> None:
    plan_id = client.post(LIST_PATH, json={"goal": "create a judge"}).json()["data"]["plan"][
        "plan_id"
    ]
    assert client.post(APPROVE_PATH.replace("{plan_id}", plan_id)).status_code == 200
    again = client.post(APPROVE_PATH.replace("{plan_id}", plan_id))
    assert again.status_code == 409


def test_route_rejects_unknown_autonomy(client: TestClient) -> None:
    resp = client.post(LIST_PATH, json={"goal": "x", "autonomy": "yolo"})
    assert resp.status_code == 400


def test_route_get_404(client: TestClient) -> None:
    assert client.get(DETAIL_PATH.replace("{plan_id}", "PLAN-missing")).status_code == 404


def test_route_cross_user_plan_access_is_404(client: TestClient) -> None:
    """A plan is visible only to its owner (and admins). A different non-admin
    user must get 404 — not another user's plan by a guessed id — on read and on
    every state-changing action (the scoped get_plan returns None first).
    """
    # @viewer / @intruder are NOT in the test admin list → real visibility scoping.
    owner = {"X-CALIBER-User": "@viewer"}
    intruder = {"X-CALIBER-User": "@intruder"}
    plan_id = client.post(LIST_PATH, json={"goal": "create a judge"}, headers=owner).json()["data"][
        "plan"
    ]["plan_id"]

    detail = DETAIL_PATH.replace("{plan_id}", plan_id)
    # Owner can read; intruder cannot.
    assert client.get(detail, headers=owner).status_code == 200
    assert client.get(detail, headers=intruder).status_code == 404
    # Every state-changing action is 404 for the intruder too.
    assert client.patch(detail, json={"autonomy": "ask_each"}, headers=intruder).status_code == 404
    assert (
        client.post(APPROVE_PATH.replace("{plan_id}", plan_id), headers=intruder).status_code == 404
    )
    assert (
        client.post(EXECUTE_PATH.replace("{plan_id}", plan_id), headers=intruder).status_code == 404
    )
    assert client.post(POLL_PATH.replace("{plan_id}", plan_id), headers=intruder).status_code == 404
    assert (
        client.get(INTERACTIONS_PATH.replace("{plan_id}", plan_id), headers=intruder).status_code
        == 404
    )
    # Admin (default @test) bypasses scoping.
    assert client.get(detail).status_code == 200


def test_route_execute_and_poll_deny_a_project_viewer_but_allow_an_editor(
    client: TestClient, db_session: Session
) -> None:
    """`P2` (isolation closure, item 7): visibility alone (project membership)
    let any active member -- including a plain ``viewer`` -- execute or poll a
    teammate's plan. ``resource.execute``'s role floor (owner/editor/reviewer)
    now independently gates both actions, matching what
    ``create_workflow_run`` already enforces for the equivalent REST action.
    """
    project_id = "P-aria-authz"
    db_session.add(CaliberProject(project_id=project_id, name="aria authz", owner="@test"))
    db_session.add_all(
        [
            CaliberProjectMember(
                member_id="M-aria-viewer",
                project_id=project_id,
                user_id="@aria-viewer",
                role=ROLE_VIEWER,
                created_by="@test",
            ),
            CaliberProjectMember(
                member_id="M-aria-editor",
                project_id=project_id,
                user_id="@aria-editor",
                role=ROLE_EDITOR,
                created_by="@test",
            ),
        ]
    )
    db_session.commit()

    created = client.post(
        LIST_PATH,
        json={"goal": "create a judge"},
        headers={"X-CALIBER-Project": project_id},
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["data"]["plan"]["plan_id"]
    assert client.post(APPROVE_PATH.replace("{plan_id}", plan_id)).status_code == 200

    execute = EXECUTE_PATH.replace("{plan_id}", plan_id)
    poll = POLL_PATH.replace("{plan_id}", plan_id)
    viewer_headers = {"X-CALIBER-User": "@aria-viewer", "X-CALIBER-Project": project_id}
    editor_headers = {"X-CALIBER-User": "@aria-editor", "X-CALIBER-Project": project_id}

    # A plain viewer is visible (project membership grants visibility) but not
    # permitted to execute or poll -- the role floor, not the visibility
    # filter, is what denies here.
    denied_execute = client.post(execute, headers=viewer_headers)
    assert denied_execute.status_code == 403, denied_execute.text
    denied_poll = client.post(poll, headers=viewer_headers)
    assert denied_poll.status_code == 403, denied_poll.text

    # An editor -- one of `resource.execute`'s permitted roles -- succeeds:
    # the authorization gate passes and the executor actually runs (whether
    # the step then completes or pauses for missing-input clarification is
    # the executor's own concern, covered elsewhere -- what matters here is
    # that it is not refused).
    executed = client.post(execute, headers=editor_headers)
    assert executed.status_code == 200, executed.text
    assert executed.json()["data"]["plan"]["status"] in ("completed", "paused", "running")


def test_route_execute_records_authorization_snapshot_once(
    client: TestClient, db_session: Session
) -> None:
    """`P2-E`: the first successful `execute`/`poll` authorization check
    persists a first-class snapshot onto the plan row -- mirroring
    `CaliberWorkspaceReleaseDecision.actor_role_snapshot`/
    `.effective_scope_snapshot` one level up (a plan, not a release). It
    records what *first* authorized the plan, so a later call that re-passes
    the still-live check must not overwrite it.
    """
    project_id = "P-aria-snapshot"
    db_session.add(CaliberProject(project_id=project_id, name="aria snapshot", owner="@test"))
    db_session.add(
        CaliberProjectMember(
            member_id="M-aria-snap-editor",
            project_id=project_id,
            user_id="@snap-editor",
            role=ROLE_EDITOR,
            created_by="@test",
        )
    )
    db_session.commit()

    created = client.post(
        LIST_PATH, json={"goal": "create a judge"}, headers={"X-CALIBER-Project": project_id}
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["data"]["plan"]["plan_id"]
    assert client.post(APPROVE_PATH.replace("{plan_id}", plan_id)).status_code == 200

    editor_headers = {"X-CALIBER-User": "@snap-editor", "X-CALIBER-Project": project_id}
    execute = EXECUTE_PATH.replace("{plan_id}", plan_id)
    poll = POLL_PATH.replace("{plan_id}", plan_id)

    assert client.post(execute, headers=editor_headers).status_code == 200

    plan_row = db_session.get(CaliberAriaPlan, plan_id)
    assert plan_row is not None
    assert plan_row.actor_role_snapshot == {"role": ROLE_EDITOR}
    assert plan_row.effective_scope_snapshot is not None
    assert plan_row.effective_scope_snapshot["policy_version"] == POLICY_VERSION
    assert isinstance(plan_row.effective_scope_snapshot["scopes"], list)
    first_recorded_at = plan_row.authorization_recorded_at
    assert first_recorded_at is not None
    db_session.commit()  # release the read transaction so the next read is fresh

    # A second call (poll) re-derives and re-passes the live check but must
    # not overwrite the already-recorded snapshot.
    assert client.post(poll, headers=editor_headers).status_code == 200
    db_session.commit()
    refreshed = db_session.get(CaliberAriaPlan, plan_id)
    assert refreshed is not None
    assert refreshed.authorization_recorded_at == first_recorded_at


def test_route_execute_denied_viewer_does_not_record_snapshot(
    client: TestClient, db_session: Session
) -> None:
    """A refused caller never reaches the persistence step -- the snapshot is
    only ever written alongside a successful authorization decision, not a
    denied one."""
    project_id = "P-aria-snap-denied"
    db_session.add(CaliberProject(project_id=project_id, name="aria snap denied", owner="@test"))
    db_session.add(
        CaliberProjectMember(
            member_id="M-aria-snap-viewer",
            project_id=project_id,
            user_id="@snap-viewer",
            role=ROLE_VIEWER,
            created_by="@test",
        )
    )
    db_session.commit()

    created = client.post(
        LIST_PATH, json={"goal": "create a judge"}, headers={"X-CALIBER-Project": project_id}
    )
    plan_id = created.json()["data"]["plan"]["plan_id"]
    assert client.post(APPROVE_PATH.replace("{plan_id}", plan_id)).status_code == 200

    viewer_headers = {"X-CALIBER-User": "@snap-viewer", "X-CALIBER-Project": project_id}
    denied = client.post(EXECUTE_PATH.replace("{plan_id}", plan_id), headers=viewer_headers)
    assert denied.status_code == 403

    plan_row = db_session.get(CaliberAriaPlan, plan_id)
    assert plan_row is not None
    assert plan_row.authorization_recorded_at is None
    assert plan_row.actor_role_snapshot is None
    assert plan_row.effective_scope_snapshot is None


def test_route_execute_personal_plan_records_scope_snapshot_without_role(
    client: TestClient, db_session: Session
) -> None:
    """A personal (`project_id is None`) plan has no project role to snapshot
    -- `actor_role_snapshot` stays `None` -- but the scope snapshot (the part
    a capability dispatch's own scope floor relies on regardless of project
    scoping) is still recorded.
    """
    created = client.post(LIST_PATH, json={"goal": "create a judge"})
    plan_id = created.json()["data"]["plan"]["plan_id"]
    assert client.post(APPROVE_PATH.replace("{plan_id}", plan_id)).status_code == 200
    assert client.post(EXECUTE_PATH.replace("{plan_id}", plan_id)).status_code == 200

    plan_row = db_session.get(CaliberAriaPlan, plan_id)
    assert plan_row is not None
    assert plan_row.project_id is None
    assert plan_row.actor_role_snapshot is None
    assert plan_row.effective_scope_snapshot is not None
    assert plan_row.effective_scope_snapshot["policy_version"] == POLICY_VERSION
    assert plan_row.authorization_recorded_at is not None


def test_route_patch_autonomy_then_list(client: TestClient, db_session: Session) -> None:
    plan_id = client.post(LIST_PATH, json={"goal": "create a judge"}).json()["data"]["plan"][
        "plan_id"
    ]
    patched = client.patch(DETAIL_PATH.replace("{plan_id}", plan_id), json={"autonomy": "ask_each"})
    assert patched.status_code == 200
    assert patched.json()["data"]["plan"]["autonomy"] == "ask_each"
    listed = client.get(LIST_PATH).json()["data"]
    assert any(p["plan_id"] == plan_id for p in listed)


def test_route_list_filters_by_session_id(client: TestClient) -> None:
    """The chat panel scopes a session's inline plans via ?session_id=."""
    in_session = client.post(
        LIST_PATH, json={"goal": "create a judge", "session_id": "SESS-1"}
    ).json()["data"]["plan"]["plan_id"]
    other = client.post(LIST_PATH, json={"goal": "create a review queue"}).json()["data"]["plan"][
        "plan_id"
    ]

    scoped = client.get(f"{LIST_PATH}?session_id=SESS-1").json()["data"]
    ids = {p["plan_id"] for p in scoped}
    assert in_session in ids
    assert other not in ids

    all_plans = {p["plan_id"] for p in client.get(LIST_PATH).json()["data"]}
    assert {in_session, other} <= all_plans
