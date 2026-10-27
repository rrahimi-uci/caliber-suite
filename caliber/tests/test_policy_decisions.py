"""A grant never exceeds the role of the person it is issued for (B03.2, #664; the decision itself is B03.3, #671).

The closed action vocabulary and the explicit set of roles each action allows are ``caliber.resource_access.
PROJECT_ACTIONS``. A grant may name only actions the subject's *current* role is in the set for, and every use
rechecks the current role, so a demotion narrows a standing grant at once. No ordering of roles or actions is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProject, CaliberProjectMember
from caliber.policy.grants import ROLE_NOT_ALLOWED, GrantError
from caliber.resource_access import (
    OWNER_ROLE_REQUIRED_SCOPES,
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
)
from tests.dialect_helpers import DIALECTS
from tests.test_run_grants import _check, _denied, _issue, _migrated, _seed

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("role", [ROLE_VIEWER, ROLE_REVIEWER, ROLE_EDITOR, ROLE_OWNER])
def test_grant_never_exceeds_role(
    dialect: str, role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        if role == ROLE_OWNER:
            run_id = _seed(engine, member=None)
            with Session(engine) as session, session.begin():
                session.get(CaliberProject, "proj-1").owner = "alice"  # type: ignore[union-attr]
            scopes: frozenset[str] | None = OWNER_ROLE_REQUIRED_SCOPES
        else:
            run_id = _seed(engine, role=role)
            scopes = None

        granted: list[tuple[str, str]] = []
        for action, roles in PROJECT_ACTIONS.items():
            if role in roles:
                grant_id = _issue(engine, run_id, allowed_actions=[action], scopes=scopes)
                assert _check(engine, run_id, grant_id, action, scopes=scopes).allowed_actions == {
                    action
                }
                granted.append((action, grant_id))
            else:
                with pytest.raises(GrantError, match="never exceeds the role"):
                    _issue(engine, run_id, allowed_actions=[action], scopes=scopes)
        assert granted, f"the {role} role is allowed some action"

        # One grant naming a permitted and a forbidden action is refused as a whole.
        permitted = next(a for a, r in PROJECT_ACTIONS.items() if role in r)
        forbidden = [a for a, r in PROJECT_ACTIONS.items() if role not in r]
        if forbidden:
            with pytest.raises(GrantError, match="never exceeds the role"):
                _issue(engine, run_id, allowed_actions=[permitted, forbidden[0]], scopes=scopes)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_demotion_narrows_a_standing_grant_at_once(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _migrated(dialect, tmp_path, monkeypatch) as (engine, _):
        run_id = _seed(engine, role=ROLE_EDITOR)
        editor_only = [
            a
            for a, roles in PROJECT_ACTIONS.items()
            if ROLE_EDITOR in roles and ROLE_VIEWER not in roles
        ]
        shared = [
            a
            for a, roles in PROJECT_ACTIONS.items()
            if ROLE_EDITOR in roles and ROLE_VIEWER in roles
        ]
        assert editor_only and shared
        grant_id = _issue(engine, run_id, allowed_actions=[editor_only[0], shared[0]])
        assert _check(engine, run_id, grant_id, editor_only[0])
        with Session(engine) as session, session.begin():
            session.query(CaliberProjectMember).filter_by(user_id="alice").one().role = ROLE_VIEWER
        with _denied(ROLE_NOT_ALLOWED):
            _check(engine, run_id, grant_id, editor_only[0])
        assert _check(engine, run_id, grant_id, shared[0]), (
            "an action the viewer role may perform is still allowed"
        )
        with Session(engine) as session, session.begin():  # promoted again: nothing was cached
            session.query(CaliberProjectMember).filter_by(user_id="alice").one().role = ROLE_EDITOR
        assert _check(engine, run_id, grant_id, editor_only[0])


def test_an_action_nobody_may_be_granted_stays_out_of_the_vocabulary() -> None:
    """``release.break_glass_apply`` is deliberately not in ``PROJECT_ACTIONS`` (no ordinary role grant), so no
    grant can name it."""
    assert "release.break_glass_apply" not in PROJECT_ACTIONS
