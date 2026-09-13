"""Integration tests for `P1-D`'s closed action registry wiring: proves
`resource.execute` (`routes/workflow_runs.py`, `routes/evaluations.py`) and
`feedback.submit` (`routes/review_queues.py`) actually gate a project-scoped
resource by role, and that the same routes still no-op correctly for a
personal/global (unscoped) resource -- the two things `resource_access.py`'s
own unit tests (`test_resource_access.py`) can't prove on their own, since
they exercise `decide_project_access` directly rather than a real route.

Two distinct denial shapes appear below, matching the two authorization
layers a request actually passes through:

* A second **admin** with no project membership reaches
  `require_project_access_if_scoped` at all only because `caliber.admin`
  bypasses the separate resource-*visibility* filter (`db/scoping.py`)
  unconditionally -- this isolates the project-*role* check from that
  visibility filter and proves it is a real, independent gate: without it,
  an unrelated admin could execute/submit-feedback against any project's
  resource. `routes/evaluations.py::create_evaluation` never applies a
  visibility filter to its dataset lookup at all, so an ordinary non-member
  reaches the same check directly there.
* A genuine project **viewer** member is visible (membership grants
  visibility) but denied by role, proving the check denies a real, if
  under-privileged, member -- not just a stranger.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberProject,
    CaliberProjectMember,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER
from caliber.review.writeback import FakeReviewWriteBackClient
from caliber.routes.review_queues import ITEMS_PATH, SUBMIT_PATH
from caliber.routes.review_queues import LIST_PATH as QUEUE_LIST_PATH
from tests.workflow_helpers import PREFIX, create_draft, make_support_manifest, register_demo_tools

PROJECT_ID = "P-p1d-wiring"


def _seed_project(session: Session, *, owner: str = "@test") -> None:
    session.add(CaliberProject(project_id=PROJECT_ID, name="P1-D wiring project", owner=owner))
    session.commit()


def _add_member(session: Session, user_id: str, role: str) -> None:
    session.add(
        CaliberProjectMember(
            member_id=f"M-{user_id.lstrip('@')}",
            project_id=PROJECT_ID,
            user_id=user_id,
            role=role,
            created_by="@test",
        )
    )
    session.commit()


def _grant_operator(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.operator_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(
        update={"operator_users": ",".join(sorted(existing))}
    )


def _grant_admin(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.admin_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(update={"admin_users": ",".join(sorted(existing))})


def _enable_queue(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )


# --------------------------------------------------------------------------
# workflow_runs.py::create_workflow_run -- `resource.execute`
# --------------------------------------------------------------------------


def _project_scoped_version(client: TestClient) -> tuple[str, str]:
    """A published workflow version whose workflow row is project-scoped."""
    register_demo_tools(client)
    created = client.post(
        f"{PREFIX}/workflows",
        json={"name": "P1-D workflow", "owner": "@test"},
        headers={"X-CALIBER-Project": PROJECT_ID},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    version_id, _ = create_draft(client, workflow_id, make_support_manifest(workflow_id))
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    return workflow_id, version_id


def test_create_workflow_run_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    """An admin with no real membership bypasses the resource-visibility
    filter but is still denied by the separate project-role check -- proof
    the `resource.execute` wiring is a real, independent gate rather than a
    no-op riding on the visibility filter's own admin bypass."""
    _seed_project(db_session)
    _wid, vid = _project_scoped_version(client)
    _enable_queue(client)
    _grant_admin(client, "@admin2")

    resp = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hi"},
        headers={"X-CALIBER-User": "@admin2"},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_workflow_run_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _wid, vid = _project_scoped_version(client)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _enable_queue(client)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hi"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_create_workflow_run_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _wid, vid = _project_scoped_version(client)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _enable_queue(client)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": vid, "alias": "manual", "input": "hi"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 202, resp.text


def test_create_workflow_run_still_works_for_an_unscoped_workflow(client: TestClient) -> None:
    """The no-op path: a personal/global workflow (`project_id is None`) is
    untouched by this slice -- a caller with no project relationship at all
    can still queue a run against it, same as before `P1-D`."""
    register_demo_tools(client)
    created = client.post(
        f"{PREFIX}/workflows", json={"name": "Personal workflow", "owner": "@test"}
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    version_id, _ = create_draft(client, workflow_id, make_support_manifest(workflow_id))
    assert client.post(f"{PREFIX}/workflow-versions/{version_id}/publish").status_code == 200
    _enable_queue(client)

    resp = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": version_id, "alias": "manual", "input": "hi"},
    )
    assert resp.status_code == 202, resp.text


# --------------------------------------------------------------------------
# evaluations.py::create_evaluation -- `resource.execute`
# --------------------------------------------------------------------------


def _seed_project_scoped_dataset(session: Session) -> str:
    dataset = CaliberEvalDataset(
        dataset_id="ED-p1d-wiring",
        name="p1d-wiring-dataset",
        owner="@test",
        version=1,
        status="active",
        project_id=PROJECT_ID,
    )
    session.add(dataset)
    session.flush()
    session.add(
        CaliberEvalDatasetExample(
            example_id="EX-p1d-1",
            dataset_id=dataset.dataset_id,
            dataset_version=1,
            input={"question": "capital of France"},
            expected={"expected": "Paris"},
        )
    )
    session.commit()
    return dataset.dataset_id


def _fake_completion(_config):
    def complete(_system: str, user: str) -> str:
        return {"capital of France": "Paris"}.get(user.strip(), "I don't know")

    return complete


def test_create_evaluation_denies_a_non_member(client: TestClient, db_session: Session) -> None:
    """`create_evaluation` looks its dataset up with a bare `session.get` --
    no visibility filter at all -- so an ordinary non-member (not an admin)
    reaches `require_project_access_if_scoped` directly here."""
    _seed_project(db_session)
    dataset_id = _seed_project_scoped_dataset(db_session)
    _grant_operator(client, "@stranger")

    resp = client.post(
        f"{PREFIX}/evaluations",
        json={"dataset_id": dataset_id, "scorers": ["exact_match"]},
        headers={"X-CALIBER-User": "@stranger"},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_create_evaluation_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    dataset_id = _seed_project_scoped_dataset(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        f"{PREFIX}/evaluations",
        json={"dataset_id": dataset_id, "scorers": ["exact_match"]},
        headers={"X-CALIBER-User": "@viewer-user"},
    )
    assert resp.status_code == 403, resp.text


def test_create_evaluation_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    import caliber.routes.evaluations as evaluations_route

    _seed_project(db_session)
    dataset_id = _seed_project_scoped_dataset(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(
        f"{PREFIX}/evaluations",
        json={"dataset_id": dataset_id, "scorers": ["exact_match"]},
        headers={"X-CALIBER-User": "@editor-user"},
    )
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------
# review_queues.py::submit_item -- `feedback.submit`
# --------------------------------------------------------------------------

_QUEUE_PAYLOAD = {
    "name": "p1d-wiring-queue",
    "description": "P1-D wiring coverage.",
    "questions": [{"key": "correct", "title": "Is the answer correct?", "type": "pass_fail"}],
    "reviewers": ["@sarah"],
}


def _project_scoped_queue(client: TestClient) -> tuple[str, str]:
    created = client.post(
        QUEUE_LIST_PATH, json=_QUEUE_PAYLOAD, headers={"X-CALIBER-Project": PROJECT_ID}
    )
    assert created.status_code == 201, created.text
    queue_id = created.json()["data"]["queue_id"]
    added = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    assert added.status_code == 201, added.text
    item_id = added.json()["data"][0]["item_id"]
    return queue_id, item_id


def test_submit_item_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    queue_id, item_id = _project_scoped_queue(client)
    _grant_admin(client, "@admin2")

    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": {"correct": True}},
        headers={"X-CALIBER-User": "@admin2"},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_submit_item_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    queue_id, item_id = _project_scoped_queue(client)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": {"correct": True}},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_submit_item_allows_a_project_reviewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    queue_id, item_id = _project_scoped_queue(client)
    _add_member(db_session, "@reviewer-user", ROLE_REVIEWER)
    _grant_operator(client, "@reviewer-user")

    client.app.state.review_writeback_client = FakeReviewWriteBackClient()
    try:
        resp = client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": True}},
            headers={"X-CALIBER-User": "@reviewer-user", "X-CALIBER-Project": PROJECT_ID},
        )
    finally:
        client.app.state.review_writeback_client = None
    assert resp.status_code == 200, resp.text


def test_submit_item_still_works_for_an_unscoped_queue(client: TestClient) -> None:
    """The no-op path: a personal/global review queue (`project_id is
    None`) is untouched -- `feedback.submit` is new in this slice, so
    without this test nothing proves the queue this action was carved out
    for still accepts a submission with no project relationship at all."""
    created = client.post(QUEUE_LIST_PATH, json=_QUEUE_PAYLOAD)
    assert created.status_code == 201, created.text
    queue_id = created.json()["data"]["queue_id"]
    added = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = added.json()["data"][0]["item_id"]

    client.app.state.review_writeback_client = FakeReviewWriteBackClient()
    try:
        resp = client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": True}},
        )
    finally:
        client.app.state.review_writeback_client = None
    assert resp.status_code == 200, resp.text
