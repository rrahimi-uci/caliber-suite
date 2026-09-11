"""Central project/resource authorization policy tests."""

from __future__ import annotations

import pytest

from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import CaliberProject, CaliberProjectMember
from caliber.resource_access import (
    ACCESS_REASONS,
    POLICY_VERSION,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
    authorize,
    decide_project_access,
    permissions_for_role,
)


def _identity(user_id: str, *, admin: bool = False) -> CaliberIdentity:
    scopes = {SCOPE_VIEWER}
    if admin:
        scopes.add(SCOPE_ADMIN)
    return CaliberIdentity(user_id=user_id, scopes=frozenset(scopes), active_project_id="P1")


def test_project_roles_have_expected_action_boundaries(db_session) -> None:
    project = CaliberProject(project_id="P1", name="one", owner="@owner")
    db_session.add(project)
    db_session.add_all(
        [
            CaliberProjectMember(
                member_id="M-editor",
                project_id="P1",
                user_id="@editor",
                role=ROLE_EDITOR,
                created_by="@owner",
            ),
            CaliberProjectMember(
                member_id="M-reviewer",
                project_id="P1",
                user_id="@reviewer",
                role=ROLE_REVIEWER,
                created_by="@owner",
            ),
            CaliberProjectMember(
                member_id="M-viewer",
                project_id="P1",
                user_id="@viewer",
                role=ROLE_VIEWER,
                created_by="@owner",
            ),
        ]
    )
    db_session.commit()

    assert decide_project_access(
        db_session, _identity("@editor"), project, "resource.write"
    ).allowed
    assert decide_project_access(
        db_session, _identity("@editor"), project, "resource.publish"
    ).allowed
    assert not decide_project_access(
        db_session, _identity("@editor"), project, "resource.approve"
    ).allowed
    assert decide_project_access(
        db_session, _identity("@reviewer"), project, "resource.approve"
    ).allowed
    assert not decide_project_access(
        db_session, _identity("@viewer"), project, "resource.write"
    ).allowed


def test_owner_has_management_permissions_admin_alone_does_not(db_session) -> None:
    """`P1-B`: `caliber.admin` is deliberately not an implicit workspace
    role (section 5.4) -- an admin with no real ownership/membership is
    denied, the direct regression-proving test for the bypass removal.
    Bare fail-closed deny, by this ticket's own explicit design: no
    replacement recovery path exists yet (that's `P1-C`/Phase 5's job)."""
    project = CaliberProject(project_id="P2", name="two", owner="@owner")
    db_session.add(project)
    db_session.commit()

    assert decide_project_access(
        db_session, _identity("@owner"), project, "project.manage_members"
    ).allowed
    admin_decision = decide_project_access(
        db_session, _identity("@admin", admin=True), project, "project.manage_members"
    )
    assert not admin_decision.allowed
    assert admin_decision.role is None
    assert admin_decision.reason == "project_access_denied"
    assert "project.manage_members" not in permissions_for_role(ROLE_VIEWER)


def test_every_decision_carries_a_closed_reason_and_the_current_policy_version(
    db_session,
) -> None:
    """`P1-B` (section 5.4): every `AccessDecision` carries a stable reason
    code from a closed vocabulary, plus `policy_version` -- across a
    representative matrix covering all four reason outcomes."""
    project = CaliberProject(project_id="P3", name="three", owner="@owner")
    db_session.add(project)
    db_session.add(
        CaliberProjectMember(
            member_id="M-viewer3",
            project_id="P3",
            user_id="@viewer3",
            role=ROLE_VIEWER,
            created_by="@owner",
        )
    )
    db_session.commit()

    decisions = [
        decide_project_access(db_session, _identity("@owner"), project, "read"),  # granted
        decide_project_access(db_session, _identity("@owner"), None, "read"),  # not found
        decide_project_access(db_session, _identity("@stranger"), project, "read"),  # no membership
        decide_project_access(
            db_session, _identity("@viewer3"), project, "project.update"
        ),  # permission denied
    ]
    for decision in decisions:
        assert decision.reason in ACCESS_REASONS
        assert decision.policy_version == POLICY_VERSION


class TestAuthorize:
    """`authorize()` -- the section 5.4 decision service `require_project_access`
    now wraps."""

    def test_delegates_to_the_role_permission_conjunct(self, db_session) -> None:
        project = CaliberProject(project_id="P4", name="four", owner="@owner")
        db_session.add(project)
        db_session.commit()

        decision = authorize(db_session, _identity("@owner"), "read", "P4")
        assert decision.allowed
        assert decision.role == ROLE_OWNER

    def test_no_workspace_id_denies_for_ordinary_actions(self, db_session) -> None:
        decision = authorize(db_session, _identity("@owner"), "read", None)
        assert not decision.allowed
        assert decision.reason == "project_not_found"

    def test_project_create_with_no_workspace_id_is_the_documented_exception(
        self, db_session
    ) -> None:
        """Section 5.4: `workspace_id=None` is valid *only* for
        `project.create`'s pre-membership bootstrap check -- confirmed as a
        GitHub Copilot finding on this PR (an earlier version denied this
        case unconditionally, contradicting the documented contract and its
        own test)."""
        no_scopes = CaliberIdentity(user_id="@nobody", scopes=frozenset({SCOPE_VIEWER}))
        denied = authorize(db_session, no_scopes, "project.create", None)
        assert not denied.allowed
        assert denied.reason == "permission_denied"

        both_scopes = CaliberIdentity(
            user_id="@both", scopes=frozenset({SCOPE_VIEWER, SCOPE_OPERATOR, SCOPE_APPROVER})
        )
        granted = authorize(db_session, both_scopes, "project.create", None)
        assert granted.allowed
        assert granted.reason == "granted"

    def test_a_real_admin_identity_satisfies_the_bootstrap_check(self, db_session) -> None:
        """GitHub Copilot review: a `CaliberIdentity` built directly with
        only the literal `SCOPE_ADMIN` value (bypassing `resolve_identity`)
        would not satisfy the two-scope check above. That is not a bug in
        `authorize()` -- `auth.py::current_scopes()` always expands
        `caliber.admin` into every scope it implies (operator, approver,
        viewer) *before* a real `CaliberIdentity` is ever constructed, the
        same assumption every other scope check in this codebase makes
        (`CaliberIdentity.has_scope` has no hierarchy logic of its own).
        This test builds the identity the way `current_scopes()` actually
        would, proving the real-world case works."""
        real_admin = CaliberIdentity(
            user_id="@admin",
            scopes=frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER}),
        )
        decision = authorize(db_session, real_admin, "project.create", None)
        assert decision.allowed

    def test_unmodeled_context_guard_runs_even_for_the_bootstrap_case(self, db_session) -> None:
        """GitHub Copilot review: the `project.create`/`workspace_id=None`
        branch used to return before the `resource`/`environment`/`release`
        guards, so a caller could bypass the "not modeled yet" signal by
        combining it with the bootstrap case. The guards must run first,
        unconditionally."""
        both_scopes = CaliberIdentity(
            user_id="@both", scopes=frozenset({SCOPE_VIEWER, SCOPE_OPERATOR, SCOPE_APPROVER})
        )
        with pytest.raises(NotImplementedError, match="per-resource"):
            authorize(db_session, both_scopes, "project.create", None, resource=object())

    def test_unknown_workspace_id_is_project_not_found(self, db_session) -> None:
        decision = authorize(db_session, _identity("@owner"), "read", "P-nope")
        assert not decision.allowed
        assert decision.reason == "project_not_found"

    @pytest.mark.parametrize(
        ("kwarg", "expected_substring"),
        [
            ("resource", "per-resource"),
            ("environment", "environment-policy"),
            ("release", "release-instance"),
        ],
    )
    def test_a_not_yet_modeled_context_raises_rather_than_silently_passing(
        self, db_session, kwarg: str, expected_substring: str
    ) -> None:
        """No `ResourceContext`/`EnvironmentContext`/`ReleaseContext` type
        exists yet -- a caller passing one anyway must get a loud
        `NotImplementedError`, not a decision that silently ignored it."""
        with pytest.raises(NotImplementedError, match=expected_substring):
            authorize(db_session, _identity("@owner"), "read", "P1", **{kwarg: object()})
