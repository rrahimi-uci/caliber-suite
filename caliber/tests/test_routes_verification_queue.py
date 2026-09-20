"""Integration tests for ``/caliber/verification-queue`` — Stage ① Verify.

See ``caliber/src/caliber/routes/verification.py``'s module docstring for the
scope this wires up (and the scope it deliberately does not: verifying an
item never creates a ``CaliberRefinementJob`` here).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberProject,
    CaliberProjectMember,
    CaliberVerificationItem,
)
from caliber.routes.verification import BATCH_PATH, DETAIL_PATH, LIST_PATH

_AGENT_ID = "support-agent"

# --- `P2-Q` (docs/workspace-plan.md's own row): CaliberVerificationItem
# previously had no project_id at all -- every operator could list, get,
# verify, dismiss, mark-duplicate, or batch-act on every project's
# manually-flagged items. These mirror test_routes_releases.py's `P2-R`
# cross-project isolation coverage shape exactly. ---------------------------

_STRANGER = {"X-CALIBER-User": "@stranger"}


def _grant_stranger_operator_scope(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@stranger"}
    )


def _seed_agent(db_session: Session, agent_id: str = _AGENT_ID) -> None:
    db_session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"exp-{agent_id}",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.commit()


def _seed_scoped_agent(
    db_session: Session, *, agent_id: str, project_id: str, owner: str = "@owner"
) -> None:
    db_session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"exp-{agent_id}",
            name=agent_id,
            owner=owner,
            project_id=project_id,
            visibility="project",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.commit()


def _seed_project_member(db_session: Session, *, project_id: str, user_id: str) -> None:
    db_session.add(CaliberProject(project_id=project_id, name=project_id, owner="@owner"))
    db_session.add(
        CaliberProjectMember(
            member_id=f"M-{project_id}-{user_id.lstrip('@')}",
            project_id=project_id,
            user_id=user_id,
            role="viewer",
            created_by="@owner",
        )
    )
    db_session.commit()


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "agent_id": _AGENT_ID,
        "category": "hallucination",
        "free_text": "Cited a policy section that doesn't exist.",
    }
    payload.update(overrides)
    return payload


def _create_item(client: TestClient, **overrides: object) -> str:
    resp = client.post(LIST_PATH, json=_create_payload(**overrides))
    assert resp.status_code == 201, resp.text
    item_id: str = resp.json()["data"]["item_id"]
    return item_id


def _detail_path(item_id: str) -> str:
    return DETAIL_PATH.replace("{item_id}", item_id)


def _action_path(item_id: str, action: str) -> str:
    return f"{DETAIL_PATH.replace('{item_id}', item_id)}/{action}"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_item_starts_pending(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    resp = client.post(LIST_PATH, json=_create_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["item_id"].startswith("FB-")
    assert data["status"] == "pending"
    assert data["agent_id"] == _AGENT_ID
    assert data["verified_by"] is None
    assert data["duplicate_of_id"] is None

    actions = {row.action for row in db_session.execute(select(CaliberAuditLog)).scalars().all()}
    assert "create_verification_item" in actions


def test_create_item_unknown_agent_is_404(client: TestClient, db_session: Session) -> None:
    resp = client.post(LIST_PATH, json=_create_payload(agent_id="ghost-agent"))
    assert resp.status_code == 404


def test_create_item_invalid_severity_is_400(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    resp = client.post(LIST_PATH, json=_create_payload(severity="urgent"))
    assert resp.status_code == 400


def test_create_item_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    resp = client.post(LIST_PATH, json=_create_payload(), headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def test_list_defaults_to_pending(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    pending_id = _create_item(client)
    verified_id = _create_item(client)
    client.post(_action_path(verified_id, "verify"), json={})

    resp = client.get(LIST_PATH)
    assert resp.status_code == 200
    ids = {row["item_id"] for row in resp.json()["data"]}
    assert ids == {pending_id}

    resp_all = client.get(LIST_PATH, params={"status": "all"})
    ids_all = {row["item_id"] for row in resp_all.json()["data"]}
    assert ids_all == {pending_id, verified_id}


def test_list_filters_by_severity_and_agent(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    _seed_agent(db_session, agent_id="other-agent")
    critical_id = _create_item(client, severity="critical")
    _create_item(client, severity="standard")
    _create_item(client, agent_id="other-agent")

    resp = client.get(LIST_PATH, params={"status": "all", "severity": "critical"})
    ids = {row["item_id"] for row in resp.json()["data"]}
    assert ids == {critical_id}

    resp = client.get(LIST_PATH, params={"status": "all", "agent_id": "other-agent"})
    assert len(resp.json()["data"]) == 1
    assert resp.json()["data"][0]["agent_id"] == "other-agent"


def test_list_rejects_unknown_status(client: TestClient, db_session: Session) -> None:
    resp = client.get(LIST_PATH, params={"status": "bogus"})
    assert resp.status_code == 400


def test_get_item_not_found(client: TestClient) -> None:
    resp = client.get(_detail_path("FB-missing"))
    assert resp.status_code == 404


def test_get_item(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.get(_detail_path(item_id))
    assert resp.status_code == 200
    assert resp.json()["data"]["item_id"] == item_id


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_verify_transitions_and_records_actor(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)

    resp = client.post(
        _action_path(item_id, "verify"),
        json={
            "verification_notes": "Confirmed against the source doc.",
            "refinement_target": "prompt",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    # No CaliberRefinementJob is created here -- see the route module docstring.
    assert body["job"] is None
    item = body["item"]
    assert item["status"] == "verified"
    assert item["verified_by"] == "@test"
    assert item["verified_at"] is not None
    assert item["verification_notes"] == "Confirmed against the source doc."
    assert item["refinement_target"] == "prompt"

    actions = {row.action for row in db_session.execute(select(CaliberAuditLog)).scalars().all()}
    assert "verify_verification_item" in actions


def test_verify_accepts_empty_body(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.post(_action_path(item_id, "verify"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["item"]["status"] == "verified"


def test_verify_already_verified_is_409(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    client.post(_action_path(item_id, "verify"))
    resp = client.post(_action_path(item_id, "verify"))
    assert resp.status_code == 409


def test_verify_missing_item_is_404(client: TestClient) -> None:
    resp = client.post(_action_path("FB-missing", "verify"))
    assert resp.status_code == 404


def test_verify_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.post(_action_path(item_id, "verify"), headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Dismiss / duplicate
# ---------------------------------------------------------------------------


def test_dismiss_without_duplicate(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.post(_action_path(item_id, "dismiss"), json={"reason": "not real"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "dismissed"
    assert data["verification_notes"] == "not real"
    assert data["duplicate_of_id"] is None


def test_dismiss_with_duplicate_of_id_marks_duplicate(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session)
    original_id = _create_item(client)
    dup_id = _create_item(client)
    resp = client.post(_action_path(dup_id, "dismiss"), json={"duplicate_of_id": original_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "duplicate"
    assert data["duplicate_of_id"] == original_id


def test_dismiss_already_settled_is_409(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    client.post(_action_path(item_id, "dismiss"))
    resp = client.post(_action_path(item_id, "dismiss"))
    assert resp.status_code == 409


def test_dedicated_duplicate_route(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    original_id = _create_item(client)
    dup_id = _create_item(client)
    resp = client.post(
        _action_path(dup_id, "duplicate"),
        json={"duplicate_of_id": original_id, "reason": "same trace"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "duplicate"
    assert data["duplicate_of_id"] == original_id
    assert data["verification_notes"] == "same trace"


def test_duplicate_of_self_is_400(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.post(_action_path(item_id, "dismiss"), json={"duplicate_of_id": item_id})
    assert resp.status_code == 400


def test_duplicate_of_unknown_item_is_404(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.post(_action_path(item_id, "dismiss"), json={"duplicate_of_id": "FB-missing"})
    assert resp.status_code == 404


def test_dismiss_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.post(_action_path(item_id, "dismiss"), headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_batch_verify_mixed_success_and_failure(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    ok_id = _create_item(client)
    already_verified_id = _create_item(client)
    client.post(_action_path(already_verified_id, "verify"))

    resp = client.post(
        BATCH_PATH,
        json={"action": "verify", "item_ids": [ok_id, already_verified_id, "FB-missing"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["action"] == "verify"
    assert data["requested"] == 3
    assert data["succeeded"] == 1
    assert data["failed"] == 2
    results = {row["item_id"]: row for row in data["results"]}
    assert results[ok_id]["status"] == "succeeded"
    assert results[ok_id]["linked_job_id"] is None
    assert results[already_verified_id]["status"] == "failed"
    assert results["FB-missing"]["status"] == "failed"

    # The successful half of the batch really did commit.
    refreshed = client.get(_detail_path(ok_id)).json()["data"]
    assert refreshed["status"] == "verified"


def test_batch_dismiss(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    item_ids = [_create_item(client) for _ in range(3)]
    resp = client.post(
        BATCH_PATH, json={"action": "dismiss", "item_ids": item_ids, "reason": "bulk cleanup"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["succeeded"] == 3
    assert data["failed"] == 0
    for item_id in item_ids:
        assert client.get(_detail_path(item_id)).json()["data"]["status"] == "dismissed"


def test_batch_rejects_unknown_action(client: TestClient, db_session: Session) -> None:
    resp = client.post(BATCH_PATH, json={"action": "approve", "item_ids": ["FB-x"]})
    assert resp.status_code == 400


def test_batch_rejects_too_many_items(client: TestClient, db_session: Session) -> None:
    resp = client.post(
        BATCH_PATH, json={"action": "verify", "item_ids": [f"FB-{i}" for i in range(201)]}
    )
    assert resp.status_code == 400


def test_batch_requires_operator_scope(client: TestClient, db_session: Session) -> None:
    resp = client.post(
        BATCH_PATH,
        json={"action": "verify", "item_ids": ["FB-x"]},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Reads require no scope beyond an identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path_builder", [lambda: LIST_PATH, lambda: _detail_path("FB-x")])
def test_reads_allowed_for_viewer(
    client: TestClient, db_session: Session, path_builder: object
) -> None:
    resp = client.get(path_builder(), headers={"X-CALIBER-User": "@viewer"})  # type: ignore[operator]
    # A viewer is allowed to read (404 for the missing detail path is fine --
    # the point is it isn't 403).
    assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# `P2-Q` — project isolation
# ---------------------------------------------------------------------------


def test_create_item_derives_project_id_from_its_agent(
    client: TestClient, db_session: Session
) -> None:
    _seed_scoped_agent(db_session, agent_id="scoped-agent", project_id="PRJ-create")
    item_id = _create_item(client, agent_id="scoped-agent")
    resp = client.get(_detail_path(item_id))
    assert resp.json()["data"]["project_id"] == "PRJ-create"


def test_create_item_leaves_project_id_null_for_a_bare_agent(
    client: TestClient, db_session: Session
) -> None:
    """A personal/global agent (no project of its own) has no project to
    derive one from -- the same "no target = personal/global" carve-out
    ``prompt_targets.py`` already applies elsewhere."""
    _seed_agent(db_session)
    item_id = _create_item(client)
    resp = client.get(_detail_path(item_id))
    assert resp.json()["data"]["project_id"] is None


def test_create_item_refuses_an_agent_in_another_project(
    client: TestClient, db_session: Session
) -> None:
    """`P2-Q`'s adjacent fix: the create route's agent lookup is now
    identity-scoped, so a caller with no membership in the agent's project
    can no longer point a new item at it merely by guessing its id."""
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    _grant_stranger_operator_scope(client)

    resp = client.post(LIST_PATH, json=_create_payload(agent_id="hidden-agent"), headers=_STRANGER)
    assert resp.status_code == 404, resp.text


def test_list_hides_a_project_scoped_item_from_a_non_member(
    client: TestClient, db_session: Session
) -> None:
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    hidden_id = _create_item(client, agent_id="hidden-agent")
    _grant_stranger_operator_scope(client)

    # No active project at all.
    resp = client.get(LIST_PATH, params={"status": "all"}, headers=_STRANGER)
    assert resp.status_code == 200, resp.text
    assert hidden_id not in {row["item_id"] for row in resp.json()["data"]}

    # Setting the header alone (no real membership row) must not unlock it
    # either -- the same membership check apply_visibility_filter's own
    # "project" tier performs, not a bare project_id string match.
    resp = client.get(
        LIST_PATH,
        params={"status": "all"},
        headers={**_STRANGER, "X-CALIBER-Project": "PRJ-hidden"},
    )
    assert resp.status_code == 200, resp.text
    assert hidden_id not in {row["item_id"] for row in resp.json()["data"]}


def test_list_shows_it_to_an_active_project_member(client: TestClient, db_session: Session) -> None:
    _seed_scoped_agent(db_session, agent_id="member-agent", project_id="PRJ-member")
    member_id = _create_item(client, agent_id="member-agent")
    _seed_project_member(db_session, project_id="PRJ-member", user_id="@stranger")
    _grant_stranger_operator_scope(client)

    resp = client.get(
        LIST_PATH,
        params={"status": "all"},
        headers={**_STRANGER, "X-CALIBER-Project": "PRJ-member"},
    )
    assert resp.status_code == 200, resp.text
    rows = {row["item_id"]: row for row in resp.json()["data"]}
    assert member_id in rows
    assert rows[member_id]["project_id"] == "PRJ-member"


def test_list_always_shows_a_projectless_item(client: TestClient, db_session: Session) -> None:
    _seed_agent(db_session)
    bare_id = _create_item(client)
    _grant_stranger_operator_scope(client)

    resp = client.get(LIST_PATH, params={"status": "all"}, headers=_STRANGER)
    assert resp.status_code == 200, resp.text
    rows = {row["item_id"]: row for row in resp.json()["data"]}
    assert rows[bare_id]["project_id"] is None


def test_admin_still_sees_every_projects_items(client: TestClient, db_session: Session) -> None:
    """The default test client is an admin -- the unconditional cross-project
    bypass every other read in the codebase already grants must still apply."""
    _seed_scoped_agent(db_session, agent_id="admin-visible-agent", project_id="PRJ-admin")
    admin_visible_id = _create_item(client, agent_id="admin-visible-agent")

    resp = client.get(LIST_PATH, params={"status": "all"})
    assert resp.status_code == 200, resp.text
    assert admin_visible_id in {row["item_id"] for row in resp.json()["data"]}


def test_get_item_refuses_a_stranger_for_another_projects_item(
    client: TestClient, db_session: Session
) -> None:
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    hidden_id = _create_item(client, agent_id="hidden-agent")
    _grant_stranger_operator_scope(client)

    resp = client.get(_detail_path(hidden_id), headers=_STRANGER)
    assert resp.status_code == 404, resp.text


def test_get_item_allows_an_active_project_member(client: TestClient, db_session: Session) -> None:
    _seed_scoped_agent(db_session, agent_id="member-agent", project_id="PRJ-member")
    member_id = _create_item(client, agent_id="member-agent")
    _seed_project_member(db_session, project_id="PRJ-member", user_id="@stranger")
    _grant_stranger_operator_scope(client)

    resp = client.get(
        _detail_path(member_id), headers={**_STRANGER, "X-CALIBER-Project": "PRJ-member"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["item_id"] == member_id


def test_verify_refuses_a_stranger_for_another_projects_item(
    client: TestClient, db_session: Session
) -> None:
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    hidden_id = _create_item(client, agent_id="hidden-agent")
    _grant_stranger_operator_scope(client)

    resp = client.post(_action_path(hidden_id, "verify"), json={}, headers=_STRANGER)
    assert resp.status_code == 404, resp.text

    db_session.expire_all()
    row = db_session.get(CaliberVerificationItem, hidden_id)
    assert row is not None and row.status == "pending"  # untouched by the refused attempt


def test_verify_allows_an_active_project_member(client: TestClient, db_session: Session) -> None:
    _seed_scoped_agent(db_session, agent_id="member-agent", project_id="PRJ-member")
    member_id = _create_item(client, agent_id="member-agent")
    _seed_project_member(db_session, project_id="PRJ-member", user_id="@stranger")
    _grant_stranger_operator_scope(client)

    resp = client.post(
        _action_path(member_id, "verify"),
        json={},
        headers={**_STRANGER, "X-CALIBER-Project": "PRJ-member"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["item"]["status"] == "verified"


def test_dismiss_refuses_a_stranger_for_another_projects_item(
    client: TestClient, db_session: Session
) -> None:
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    hidden_id = _create_item(client, agent_id="hidden-agent")
    _grant_stranger_operator_scope(client)

    resp = client.post(_action_path(hidden_id, "dismiss"), json={}, headers=_STRANGER)
    assert resp.status_code == 404, resp.text


def test_duplicate_refuses_linking_to_another_projects_item(
    client: TestClient, db_session: Session
) -> None:
    """Even when the caller's *own* item is in scope, the ``duplicate_of_id``
    it names must be too -- otherwise it's an oracle for another project's
    item ids and silently cross-links the queues."""
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    hidden_original_id = _create_item(client, agent_id="hidden-agent")

    _seed_agent(db_session)
    own_id = _create_item(client)
    _grant_stranger_operator_scope(client)

    resp = client.post(
        _action_path(own_id, "duplicate"),
        json={"duplicate_of_id": hidden_original_id},
        headers=_STRANGER,
    )
    assert resp.status_code == 404, resp.text


def test_batch_action_fails_closed_for_another_projects_item(
    client: TestClient, db_session: Session
) -> None:
    _seed_scoped_agent(db_session, agent_id="hidden-agent", project_id="PRJ-hidden")
    hidden_id = _create_item(client, agent_id="hidden-agent")
    _seed_agent(db_session)
    own_id = _create_item(client)
    _grant_stranger_operator_scope(client)

    resp = client.post(
        BATCH_PATH,
        json={"action": "verify", "item_ids": [own_id, hidden_id]},
        headers=_STRANGER,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    results = {row["item_id"]: row for row in data["results"]}
    assert results[own_id]["status"] == "succeeded"
    assert results[hidden_id]["status"] == "failed"

    db_session.expire_all()
    row = db_session.get(CaliberVerificationItem, hidden_id)
    assert row is not None and row.status == "pending"  # untouched by the refused attempt
