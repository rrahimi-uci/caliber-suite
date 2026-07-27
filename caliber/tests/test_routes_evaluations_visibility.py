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

A follow-up review raised two further points, both addressed here:

3. Detail and list had drifted apart. Detail admitted any row whose
   ``project_id`` matched the client-supplied ``X-CALIBER-Project`` header
   regardless of owner, and admitted the creator's rows from projects other
   than the active one — neither of which the list would return. Detail now
   resolves through :func:`caliber.db.scoping.get_visible`, so the two share
   one filter by construction.
4. The tests seeded rows directly rather than exercising the real
   create → list → detail path. ``test_created_run_*`` below drive the actual
   route, which also disproves the reviewer's claim of a "create-to-list hole":
   ``create_evaluation`` sets ``visibility="project" if project_id else "user"``
   explicitly, so the model's ``visibility="project"`` default never applies and
   a project-less run is user-visible to its creator.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.evaluations as evaluations_route
from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample, CaliberEvalRun
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


# --- detail/list parity (follow-up review point 3) ---------------------------


def test_detail_does_not_leak_another_owners_row_via_the_project_header(
    client: TestClient, session_factory: object
) -> None:
    """A project header alone must not unlock someone else's run.

    Detail previously admitted any row whose ``project_id`` matched the
    client-supplied ``X-CALIBER-Project`` header, with no owner check, while the
    list required ownership. Anyone who set the header could read another
    user's project-scoped run — including its full per-example results.
    """
    with session_factory() as session:  # type: ignore[operator]
        _seed_run(
            session,
            run_id="ER-theirs-proj",
            created_by=OTHER_USER,
            visibility="project",
            project_id="PROJ-a",
        )

    headers = {"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PROJ-a"}
    detail = client.get(DETAIL_PATH.format(run_id="ER-theirs-proj"), headers=headers)
    listed = client.get(LIST_PATH, headers=headers)

    # Both surfaces agree: not yours, not visible.
    assert detail.status_code == 404, detail.text
    assert "ER-theirs-proj" not in {i["run_id"] for i in listed.json()["data"]}


def test_detail_and_list_agree_for_a_row_outside_the_active_project(
    client: TestClient, session_factory: object
) -> None:
    """Detail must not surface the creator's row from a non-active project.

    The list scopes project rows to the active project; detail used to return
    the creator's row from *any* project, so the two disagreed.
    """
    with session_factory() as session:  # type: ignore[operator]
        _seed_run(
            session,
            run_id="ER-mine-proj-b",
            created_by=NON_ADMIN,
            visibility="project",
            project_id="PROJ-b",
        )

    headers = {"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PROJ-a"}
    detail = client.get(DETAIL_PATH.format(run_id="ER-mine-proj-b"), headers=headers)
    listed = client.get(LIST_PATH, headers=headers)

    assert detail.status_code == 404, detail.text
    assert "ER-mine-proj-b" not in {i["run_id"] for i in listed.json()["data"]}

    # ...and both agree again once that project is the active one.
    headers_b = {"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PROJ-b"}
    assert (
        client.get(DETAIL_PATH.format(run_id="ER-mine-proj-b"), headers=headers_b).status_code
        == 200
    )
    assert "ER-mine-proj-b" in {
        i["run_id"] for i in client.get(LIST_PATH, headers=headers_b).json()["data"]
    }


# --- real create -> list -> detail round trip (follow-up review point 4) -----


def _fake_completion(_config):
    def complete(_system: str, user: str) -> str:
        return "Paris"

    return complete


def _grant_operator(client: TestClient, *users: str) -> None:
    """Give the test users operator scope without making them admins.

    ``POST /evaluations`` requires ``caliber.operator``, but admin would bypass
    the visibility filter and defeat the point of the round trip.
    """
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": ",".join(users)}
    )


def _seed_dataset(session: Session) -> None:
    session.add(
        CaliberEvalDataset(
            dataset_id="ED-rt",
            name="round-trip",
            description="",
            owner=NON_ADMIN,
            tags=[],
            status="active",
            version=1,
        )
    )
    session.add(
        CaliberEvalDatasetExample(
            example_id="EX-rt",
            dataset_id="ED-rt",
            dataset_version=1,
            input={"question": "capital of France"},
            expected={"expected": "Paris"},
            weight=1.0,
            tags=[],
        )
    )
    session.commit()


def test_created_run_without_a_project_is_listable_and_readable_by_its_creator(
    client: TestClient, session_factory: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual create -> list -> detail path, with no active project.

    This is the case a reviewer believed was broken ("the creator detail branch
    can read that row, but the list filter cannot return it"). It is not:
    ``create_evaluation`` stores ``visibility="user"`` when there is no active
    project, which the list's user tier returns for its creator.
    """
    with session_factory() as session:  # type: ignore[operator]
        _seed_dataset(session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    _grant_operator(client, NON_ADMIN)

    headers = {"X-CALIBER-User": NON_ADMIN}
    created = client.post(
        LIST_PATH, json={"dataset_id": "ED-rt", "scorers": ["exact_match"]}, headers=headers
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["data"]["run_id"]
    with session_factory() as session:  # type: ignore[operator]
        stored = session.get(CaliberEvalRun, run_id)
        assert stored is not None
        # The model default is "project"; the route overrides it to "user" when
        # there is no active project. This is the line the reviewer missed.
        assert (stored.visibility, stored.project_id) == ("user", None)

    assert run_id in {i["run_id"] for i in client.get(LIST_PATH, headers=headers).json()["data"]}
    assert client.get(DETAIL_PATH.format(run_id=run_id), headers=headers).status_code == 200


def test_created_run_in_a_project_is_listable_and_readable_by_its_creator(
    client: TestClient, session_factory: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same round trip with an active project header."""
    with session_factory() as session:  # type: ignore[operator]
        _seed_dataset(session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    _grant_operator(client, NON_ADMIN, OTHER_USER)

    headers = {"X-CALIBER-User": NON_ADMIN, "X-CALIBER-Project": "PROJ-a"}
    created = client.post(
        LIST_PATH, json={"dataset_id": "ED-rt", "scorers": ["exact_match"]}, headers=headers
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["data"]["run_id"]
    with session_factory() as session:  # type: ignore[operator]
        stored = session.get(CaliberEvalRun, run_id)
        assert stored is not None
        assert (stored.visibility, stored.project_id) == ("project", "PROJ-a")

    assert run_id in {i["run_id"] for i in client.get(LIST_PATH, headers=headers).json()["data"]}
    assert client.get(DETAIL_PATH.format(run_id=run_id), headers=headers).status_code == 200

    # Another user in the same project can reach neither surface.
    other = {"X-CALIBER-User": OTHER_USER, "X-CALIBER-Project": "PROJ-a"}
    assert client.get(DETAIL_PATH.format(run_id=run_id), headers=other).status_code == 404
    assert run_id not in {i["run_id"] for i in client.get(LIST_PATH, headers=other).json()["data"]}
