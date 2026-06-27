"""Tests for the 3-tier visibility scoping helper (db/scoping.py)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import CaliberSkill
from caliber.db.scoping import apply_visibility_filter


def _skill(
    skill_id: str, name: str, owner: str, visibility: str, project_id: str | None = None
) -> CaliberSkill:
    return CaliberSkill(
        skill_id=skill_id,
        name=name,
        content="x",
        owner=owner,
        visibility=visibility,
        project_id=project_id,
    )


def _ident(user_id: str, *, project: str | None = None, admin: bool = False) -> CaliberIdentity:
    scopes = frozenset({SCOPE_ADMIN, SCOPE_VIEWER} if admin else {SCOPE_VIEWER})
    return CaliberIdentity(user_id=user_id, scopes=scopes, active_project_id=project)


def _names(
    session: Session, identity: CaliberIdentity, project_id: str | None, only=None
) -> set[str]:
    stmt = apply_visibility_filter(
        select(CaliberSkill), CaliberSkill, identity, project_id, only=only
    )
    return {row.skill_id for row in session.execute(stmt).scalars().all()}


def _seed(session: Session) -> None:
    session.add_all(
        [
            _skill("a_p1", "a-proj-p1", "@alice", "project", "P1"),
            _skill("a_p2", "a-proj-p2", "@alice", "project", "P2"),
            _skill("a_user", "a-user", "@alice", "user"),
            _skill("b_p1", "b-proj-p1", "@bob", "project", "P1"),
            _skill("b_user", "b-user", "@bob", "user"),
            _skill("pub", "pub", "@bob", "public"),
        ]
    )
    session.commit()


def test_project_tier_returns_only_owners_rows_in_active_project(db_session: Session) -> None:
    _seed(db_session)
    # @alice in P1 sees: her P1 project row + her user-library row + public.
    assert _names(db_session, _ident("@alice", project="P1"), "P1") == {"a_p1", "a_user", "pub"}


def test_other_users_project_rows_are_invisible(db_session: Session) -> None:
    _seed(db_session)
    assert _names(db_session, _ident("@bob", project="P1"), "P1") == {"b_p1", "b_user", "pub"}


def test_no_active_project_drops_the_project_tier(db_session: Session) -> None:
    _seed(db_session)
    assert _names(db_session, _ident("@alice", project=None), None) == {"a_user", "pub"}


def test_only_user_tier(db_session: Session) -> None:
    _seed(db_session)
    assert _names(db_session, _ident("@alice", project="P1"), "P1", only="user") == {"a_user"}


def test_only_public_tier(db_session: Session) -> None:
    _seed(db_session)
    assert _names(db_session, _ident("@alice", project="P1"), "P1", only="public") == {"pub"}


def test_only_project_with_no_active_project_matches_nothing(db_session: Session) -> None:
    _seed(db_session)
    assert _names(db_session, _ident("@alice", project=None), None, only="project") == set()


def test_admin_bypass_sees_every_row(db_session: Session) -> None:
    _seed(db_session)
    got = _names(db_session, _ident("@root", project="P1", admin=True), "P1")
    assert got == {"a_p1", "a_p2", "a_user", "b_p1", "b_user", "pub"}


def test_admin_with_only_filter_returns_tier_across_all_owners(db_session: Session) -> None:
    _seed(db_session)
    assert _names(db_session, _ident("@root", project="P1", admin=True), "P1", only="user") == {
        "a_user",
        "b_user",
    }
    assert _names(db_session, _ident("@root", project="P1", admin=True), "P1", only="project") == {
        "a_p1",
        "b_p1",
    }
