"""Route-level tests for multi-user project scoping.

Uses skills as the representative scoped resource (every scoped list/create
endpoint shares the same wiring). The unit-level filter logic is covered in
``test_scoping.py``; these tests assert the *route* behavior: owner comes from
the authenticated actor, visibility defaults correctly, the visibility filter is
applied to list endpoints, and ``?visibility=`` selects a single tier.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberSkill
from caliber.routes.skills import LIST_PATH


def _create(client: TestClient, name: str, *, body_owner: str = "@someone-else", headers=None):
    return client.post(
        LIST_PATH,
        json={"name": name, "content": "Think step by step.", "owner": body_owner},
        headers=headers,
    )


def _stored(session: Session, name: str) -> CaliberSkill:
    return session.execute(select(CaliberSkill).where(CaliberSkill.name == name)).scalars().one()


def _seed(session: Session, **overrides: object) -> None:
    defaults: dict[str, object] = {
        "skill_id": overrides["name"],  # reuse name as id for brevity
        "content": "x",
        "owner": "@owner",
        "status": "active",
        "version": 1,
    }
    defaults.update(overrides)
    session.add(CaliberSkill(**defaults))
    session.commit()


# ── create: owner from actor, not body ────────────────────────────────────


def test_create_sets_owner_from_actor_ignoring_body(
    client: TestClient, db_session: Session
) -> None:
    # Default client is @test; the body claims a different owner.
    resp = _create(client, "owned-by-actor", body_owner="@someone-else")
    assert resp.status_code == 201
    assert resp.json()["data"]["owner"] == "@test"  # actor, not the spoofed body value
    assert _stored(db_session, "owned-by-actor").owner == "@test"


def test_create_without_active_project_defaults_to_user_visibility(
    client: TestClient, db_session: Session
) -> None:
    _create(client, "no-project")
    row = _stored(db_session, "no-project")
    assert row.visibility == "user"
    assert row.project_id is None


def test_create_with_project_header_is_project_scoped(
    client: TestClient, db_session: Session
) -> None:
    _create(client, "in-project", headers={"X-CALIBER-Project": "PRJ-1"})
    row = _stored(db_session, "in-project")
    assert row.visibility == "project"
    assert row.project_id == "PRJ-1"


# ── list: visibility filter is applied ─────────────────────────────────────


def test_non_admin_sees_only_public_across_owners(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="pub", owner="@x", visibility="public")
    _seed(db_session, name="user-x", owner="@x", visibility="user")
    _seed(db_session, name="proj-x", owner="@x", visibility="project", project_id="P1")

    # @viewer is non-admin, owns nothing, and has no active project.
    resp = client.get(LIST_PATH, headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()["data"]} == {"pub"}


def test_visibility_query_param_selects_single_tier(
    client: TestClient, db_session: Session
) -> None:
    _seed(db_session, name="pub", owner="@x", visibility="public")
    _seed(db_session, name="proj-x", owner="@x", visibility="project", project_id="P1")

    # Admin still gets the requested tier, but never other tiers.
    resp = client.get(LIST_PATH, params={"visibility": "public"})
    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()["data"]} == {"pub"}


def test_admin_visibility_user_includes_all_owners(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="user-a", owner="@alice", visibility="user")
    _seed(db_session, name="user-b", owner="@bob", visibility="user")
    _seed(db_session, name="pub", owner="@x", visibility="public")

    resp = client.get(LIST_PATH, params={"visibility": "user"})
    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()["data"]} == {"user-a", "user-b"}


def test_admin_without_filter_sees_everything(client: TestClient, db_session: Session) -> None:
    _seed(db_session, name="pub", owner="@x", visibility="public")
    _seed(db_session, name="proj-x", owner="@x", visibility="project", project_id="P1")

    resp = client.get(LIST_PATH)  # default @test is admin → bypass
    assert {item["name"] for item in resp.json()["data"]} == {"pub", "proj-x"}
