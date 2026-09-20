"""Route-level proof for `docs/workspace-plan.md` section 19.2's ratified
decision: "Is agent registration a Developer or an Admin action? Decided:
Developer -- authoring an agent is authoring." Before this change,
`register_agent`/`update_agent`/`delete_agent` (`routes/agents.py`) were all
gated by `SCOPE_ADMIN` alone, with no project-role check at all -- the exact
gap section 2.5.3 named ("Agent registration is admin-only ... a Developer
cannot create the record that prompts, jobs and approvals hang off").

Per section 2.5.2's per-resource target table, the widening is not uniform
across the three routes:

* ``register_agent`` (create) and ``update_agent`` (edit) move to
  `SCOPE_OPERATOR` (Developer-reachable; an admin identity still qualifies --
  `caliber.admin` implies `caliber.operator`, section 2.3) -- **except**
  ``update_agent``'s `enabled` field, this resource's release/activate lever,
  which keeps the `SCOPE_ADMIN` ceiling.
* ``delete_agent`` stays `SCOPE_ADMIN`-only -- section 2.5.2 keeps delete
  Admin-only for every runtime family.

All three routes also now compose a `resource.write.runtime` project-role
check (P2 isolation closure, extended here to the Agent family, which had
never received it), same denial-shape split
`test_p2_resource_write_action_wiring.py` established: an admin with no real
project membership is denied (proving the role check is a real, independent
gate); a genuine project viewer is denied by role; a project editor with only
the (now-sufficient) `caliber.operator` scope succeeds.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAgentConfig, CaliberProject, CaliberProjectMember
from caliber.resource_access import ROLE_EDITOR, ROLE_VIEWER
from caliber.routes.agents import DETAIL_PATH, LIST_PATH

PROJECT_ID = "P-agent-developer-scope"


def _seed_project(session: Session, *, owner: str = "@test") -> None:
    session.add(CaliberProject(project_id=PROJECT_ID, name="Agent scope project", owner=owner))
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


def _seed_agent(
    session: Session, *, agent_id: str, project_id: str | None = None, owner: str = "@test"
) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"exp-{agent_id}",
            name="Scoped agent",
            owner=owner,
            project_id=project_id,
            visibility="project" if project_id else "user",
            artifact_types=["prompt"],
            optimizer_config={},
        )
    )
    session.commit()


_REGISTER_BODY = {
    "agent_id": "dev-scoped-agent",
    "experiment_id": "exp-dev-scoped-agent",
    "name": "Dev Scoped Agent",
    "owner": "@editor-user",
}


# ---------------------------------------------------------------------------
# POST /agents -- now a Developer (`SCOPE_OPERATOR`) action
# ---------------------------------------------------------------------------


def test_register_agent_now_succeeds_for_a_plain_developer_with_no_admin_scope(
    client: TestClient,
) -> None:
    """The core proof of the ratified widening: a caller holding only
    `caliber.operator` -- never `caliber.admin` -- can register an agent,
    which was refused with a 403 before this change."""
    resp = client.post(
        LIST_PATH,
        json={
            **_REGISTER_BODY,
            "agent_id": "unscoped-dev-agent",
            "experiment_id": "exp-unscoped-dev",
        },
        headers={"X-CALIBER-User": "@developer-only"},
    )
    # A brand-new identity with no configured scopes at all is refused first --
    # grant only `caliber.operator` (never `caliber.admin`) and retry.
    assert resp.status_code == 403, resp.text
    _grant_operator(client, "@developer-only")

    resp = client.post(
        LIST_PATH,
        json={
            **_REGISTER_BODY,
            "agent_id": "unscoped-dev-agent",
            "experiment_id": "exp-unscoped-dev",
        },
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert resp.status_code == 201, resp.text


def test_register_agent_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _grant_admin(client, "@admin2")

    resp = client.post(
        LIST_PATH,
        json={**_REGISTER_BODY, "agent_id": "a-admin2"},
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert PROJECT_ID in resp.json()["detail"]


def test_register_agent_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        LIST_PATH,
        json={**_REGISTER_BODY, "agent_id": "a-viewer"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_register_agent_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        LIST_PATH,
        json={**_REGISTER_BODY, "agent_id": "a-editor"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 201, resp.text


def test_register_agent_still_works_for_an_unscoped_agent(client: TestClient) -> None:
    """No-op path: no active project means no workspace to gate against."""
    resp = client.post(
        LIST_PATH,
        json={**_REGISTER_BODY, "agent_id": "a-personal", "experiment_id": "exp-personal"},
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# PATCH /agents/{id} -- Developer for ordinary fields, Admin for `enabled`
# ---------------------------------------------------------------------------


def test_update_agent_allows_a_developer_to_edit_a_non_enabled_field(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session, agent_id="a-1", owner="@developer-only")
    resp = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "Renamed by a Developer"},
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert resp.status_code == 403, resp.text
    _grant_operator(client, "@developer-only")

    resp = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "Renamed by a Developer"},
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["name"] == "Renamed by a Developer"


def test_update_agent_enabled_toggle_still_requires_admin(
    client: TestClient, db_session: Session
) -> None:
    """`enabled` is this resource's release/activate lever (section 2.5.2) --
    a plain Developer (`caliber.operator` only) must still be refused,
    even though the same caller can edit every other field."""
    _seed_agent(db_session, agent_id="a-1", owner="@developer-only")
    _grant_operator(client, "@developer-only")

    # The same field succeeds when it isn't `enabled` ...
    ok = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "Still a developer"},
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert ok.status_code == 200, ok.text

    # ... but toggling `enabled` is refused for the same identity.
    resp = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"enabled": False},
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert resp.status_code == 403, resp.text


def test_update_agent_enabled_toggle_still_works_for_admin(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session, agent_id="a-1")
    resp = client.patch(DETAIL_PATH.replace("{agent_id}", "a-1"), json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["enabled"] is False


def test_update_agent_mixed_body_including_enabled_still_requires_admin(
    client: TestClient, db_session: Session
) -> None:
    """A request that changes `enabled` alongside an ordinary field must be
    held to the stricter floor for the whole request, not just the
    `enabled` key -- there is one scope check per request, not per field."""
    _seed_agent(db_session, agent_id="a-1", owner="@developer-only")
    _grant_operator(client, "@developer-only")

    resp = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "Sneaking enabled in", "enabled": False},
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert resp.status_code == 403, resp.text


def test_update_agent_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _seed_agent(db_session, agent_id="a-1", project_id=PROJECT_ID)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "edited"},
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_update_agent_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _seed_agent(db_session, agent_id="a-1", project_id=PROJECT_ID)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.patch(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        json={"name": "edited"},
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# DELETE /agents/{id} -- stays Admin-only, now also project-role-composed
# ---------------------------------------------------------------------------


def test_delete_agent_still_refuses_a_plain_developer(
    client: TestClient, db_session: Session
) -> None:
    _seed_agent(db_session, agent_id="a-1")
    _grant_operator(client, "@developer-only")

    resp = client.delete(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        headers={"X-CALIBER-User": "@developer-only"},
    )
    assert resp.status_code == 403, resp.text
    assert db_session.get(CaliberAgentConfig, "a-1") is not None


def test_delete_agent_denies_an_admin_with_no_project_membership(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_agent(db_session, agent_id="a-1", project_id=PROJECT_ID)
    _grant_admin(client, "@admin2")

    resp = client.delete(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        headers={"X-CALIBER-User": "@admin2", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 404, resp.text
    assert db_session.get(CaliberAgentConfig, "a-1") is not None


def test_delete_agent_denies_a_project_viewer_even_with_admin_scope(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_agent(db_session, agent_id="a-1", project_id=PROJECT_ID)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_admin(client, "@viewer-user")

    resp = client.delete(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        headers={"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 403, resp.text


def test_delete_agent_allows_a_project_editor_with_admin_scope(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_agent(db_session, agent_id="a-1", project_id=PROJECT_ID)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_admin(client, "@editor-user")

    resp = client.delete(
        DETAIL_PATH.replace("{agent_id}", "a-1"),
        headers={"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID},
    )
    assert resp.status_code == 200, resp.text
    assert db_session.get(CaliberAgentConfig, "a-1") is None
