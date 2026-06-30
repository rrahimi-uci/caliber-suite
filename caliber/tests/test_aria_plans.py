"""Phase 1 — Aria goal-plans: planner decomposition, persistence, and routes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.assistant.capabilities import Capability, registered_capabilities
from caliber.assistant.plans import HeuristicPlanner, PlannedStep, PlanService
from caliber.db.models import CaliberAriaPlan, CaliberAriaPlanStep, CaliberAuditLog
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
