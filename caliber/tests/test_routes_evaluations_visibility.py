"""Non-admin visibility contract for ``/caliber/evaluations``.

The repository-wide review (``ui-complete-report.md`` §C4) found that the whole
default test suite runs as an admin (``conftest.DEFAULT_TEST_USER`` is in
``CALIBER_ADMIN_USERS``), which hid two defects on the *non-admin* branch:

1. ``list_evaluations`` handed ``CaliberEvalRun`` to
   :func:`apply_visibility_filter`, which dereferences ``model.owner``. The
   model has ``created_by`` instead, so every non-admin list request raised
   ``AttributeError`` before SQL ran — a hard 500, not a filtered list.
2. ``get_evaluation`` allowed admins, public rows, and rows in the active
   project, but not "the row I created", so a creator could not read back their
   own user-scoped (project-less) run.

These tests pin both branches with a user that is *not* in the permissive admin
list, so the admin bypass can never mask them again.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberEvalRun
from caliber.routes.evaluations import DETAIL_PATH, LIST_PATH

# Deliberately not in ``_PERMISSIVE_TEST_USERS`` — this user holds viewer scope
# only, so it takes the non-admin branch of ``apply_visibility_filter``.
NON_ADMIN = "@viewer"
OTHER_USER = "@someone-else"


def _seed_run(
    session: Session,
    *,
    run_id: str,
    created_by: str,
    visibility: str,
    project_id: str | None = None,
) -> None:
    session.add(
        CaliberEvalRun(
            run_id=run_id,
            dataset_id="ED-x",
            dataset_version=1,
            label=run_id,
            scorers=["exact_match"],
            model="fake",
            status="completed",
            created_by=created_by,
            visibility=visibility,
            project_id=project_id,
        )
    )
    session.commit()


def test_list_evaluations_as_non_admin_does_not_crash(
    client: TestClient, session_factory: object
) -> None:
    """A viewer listing evaluations gets a filtered 200, never a 500.

    Regression for the ``AttributeError: type object 'CaliberEvalRun' has no
    attribute 'owner'`` raised by the generic visibility helper.
    """
    with session_factory() as session:  # type: ignore[operator]
        _seed_run(session, run_id="ER-public", created_by=OTHER_USER, visibility="public")
        _seed_run(session, run_id="ER-mine", created_by=NON_ADMIN, visibility="user")
        _seed_run(session, run_id="ER-theirs", created_by=OTHER_USER, visibility="user")

    resp = client.get(LIST_PATH, headers={"X-CALIBER-User": NON_ADMIN})

    assert resp.status_code == 200, resp.text
    ids = {item["run_id"] for item in resp.json()["data"]}
    # Public is visible to everyone; my own user-scoped run is visible to me;
    # another user's user-scoped run is not.
    assert "ER-public" in ids
    assert "ER-mine" in ids
    assert "ER-theirs" not in ids


def test_list_evaluations_as_non_admin_hides_other_projects(
    client: TestClient, session_factory: object
) -> None:
    """Project-scoped rows only surface inside their own active project."""
    with session_factory() as session:  # type: ignore[operator]
        _seed_run(
            session,
            run_id="ER-proj-a",
            created_by=NON_ADMIN,
            visibility="project",
            project_id="PROJ-a",
        )
        _seed_run(
            session,
            run_id="ER-proj-b",
            created_by=NON_ADMIN,
            visibility="project",
            project_id="PROJ-b",
        )

    resp = client.get(
        LIST_PATH,
        headers={"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PROJ-a"},
    )

    assert resp.status_code == 200, resp.text
    ids = {item["run_id"] for item in resp.json()["data"]}
    assert ids == {"ER-proj-a"}


def test_get_evaluation_allows_its_creator(client: TestClient, session_factory: object) -> None:
    """The creator of a user-scoped run can read it back.

    Regression for the missing ``created_by == identity.user_id`` branch: a
    project-less run created by a non-admin was unreachable by its own author.
    """
    with session_factory() as session:  # type: ignore[operator]
        _seed_run(session, run_id="ER-mine", created_by=NON_ADMIN, visibility="user")

    resp = client.get(DETAIL_PATH.format(run_id="ER-mine"), headers={"X-CALIBER-User": NON_ADMIN})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["run_id"] == "ER-mine"


def test_get_evaluation_still_hides_another_users_run(
    client: TestClient, session_factory: object
) -> None:
    """Widening the detail read to the creator must not widen it to everyone."""
    with session_factory() as session:  # type: ignore[operator]
        _seed_run(session, run_id="ER-theirs", created_by=OTHER_USER, visibility="user")

    resp = client.get(DETAIL_PATH.format(run_id="ER-theirs"), headers={"X-CALIBER-User": NON_ADMIN})

    assert resp.status_code == 404, resp.text
