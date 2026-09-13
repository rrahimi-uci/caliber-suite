"""``GET /aria/plans`` — list visibility (`P2`, isolation closure item 6).

Previously filtered by ``owner == actor`` only, regardless of the row's own
``visibility`` -- a project-shared plan was invisible to a teammate's list
even though ``GET /aria/plans/{id}`` would serve it by id directly. These
tests pin the fix: list and detail now agree on what "visible" means.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAriaPlan, CaliberProject, CaliberProjectMember

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans"
_STRANGER = {"X-CALIBER-User": "@stranger"}


def _insert_plan(session: Session, **overrides: object) -> CaliberAriaPlan:
    defaults: dict[str, object] = {
        "plan_id": "PLAN-list0001",
        "goal": "test goal",
        "owner": "@sarah",
        "status": "draft",
    }
    defaults.update(overrides)
    plan = CaliberAriaPlan(**defaults)
    session.add(plan)
    session.commit()
    return plan


def test_list_plans_hides_another_users_personal_plan(
    client: TestClient, db_session: Session
) -> None:
    """Unchanged behavior: the default ``visibility="user"`` tier still
    means "owned by me only"."""
    _insert_plan(db_session, plan_id="PLAN-personal01", owner="@sarah")
    listed = client.get(LIST_PATH, headers=_STRANGER).json()["data"]
    assert "PLAN-personal01" not in {p["plan_id"] for p in listed}


def test_list_plans_includes_a_project_shared_plan_for_a_project_member(
    client: TestClient, db_session: Session
) -> None:
    """The fix: a project-visibility plan is now listable by a teammate,
    not just its owner -- matching what ``GET /aria/plans/{id}`` already
    served."""
    db_session.add(CaliberProject(project_id="P-shared", name="Shared project", owner="@sarah"))
    db_session.add(
        CaliberProjectMember(
            member_id="M-teammate",
            project_id="P-shared",
            user_id="@teammate",
            role="viewer",
            created_by="@sarah",
        )
    )
    db_session.commit()
    _insert_plan(
        db_session,
        plan_id="PLAN-shared001",
        owner="@sarah",
        visibility="project",
        project_id="P-shared",
    )
    listed = client.get(
        LIST_PATH, headers={"X-CALIBER-User": "@teammate", "X-CALIBER-Project": "P-shared"}
    ).json()["data"]
    assert "PLAN-shared001" in {p["plan_id"] for p in listed}


def test_list_plans_still_hides_a_project_shared_plan_from_a_non_member(
    client: TestClient, db_session: Session
) -> None:
    _insert_plan(
        db_session,
        plan_id="PLAN-shared002",
        owner="@sarah",
        visibility="project",
        project_id="P-shared",
    )
    listed = client.get(
        LIST_PATH, headers={"X-CALIBER-User": "@stranger", "X-CALIBER-Project": "P-other"}
    ).json()["data"]
    assert "PLAN-shared002" not in {p["plan_id"] for p in listed}


def test_list_plans_includes_a_public_plan_for_any_authenticated_user(
    client: TestClient, db_session: Session
) -> None:
    _insert_plan(db_session, plan_id="PLAN-public001", owner="@sarah", visibility="public")
    listed = client.get(LIST_PATH, headers=_STRANGER).json()["data"]
    assert "PLAN-public001" in {p["plan_id"] for p in listed}
