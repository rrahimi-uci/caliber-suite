"""`P4-B`/`P4-C`: the fourth real Workspace resource adapter -- a CALIBER skill.

Mirrors ``test_workspace_release_judge_adapter.py``'s pattern (proving the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol against a genuine resource type) and
``test_workspace_release_prompt_adapter.py``'s explicit-version-only
resolve() coverage -- see ``workspace_release_skill_adapter.py``'s module
docstring for why a skill is a genuinely fourth resource shape: a mutable
live row *and* a real, internal, immutable, numbered content history.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberSkill,
    CaliberSkillVersion,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)
from caliber.workspace_release_skill_adapter import (
    SKILL_ADAPTER_VERSION,
    ResolvedSkillVersion,
    SkillWorkspaceResourceAdapter,
    _content_sha256,
)

PROJECT_ID = "PRJ-skill-adapter"
OTHER_PROJECT_ID = "PRJ-skill-adapter-other"
ADAPTER = SkillWorkspaceResourceAdapter()


def _identity(
    user_id: str = "@caller", *, active_project_id: str | None = None, admin: bool = False
) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user_id,
        scopes=frozenset({SCOPE_ADMIN}) if admin else frozenset(),
        active_project_id=active_project_id,
    )


def _seed_project(session: Session, *, project_id: str = PROJECT_ID) -> None:
    session.add(
        CaliberProject(project_id=project_id, name=f"Skill adapter {project_id}", owner="admin")
    )
    session.flush()


def _seed_skill(
    session: Session,
    *,
    skill_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    name: str | None = None,
    content: str = "Full instructions for the skill.",
    summary: str = "When to use this skill.",
    version: int = 1,
    status: str = "active",
) -> CaliberSkill:
    skill = CaliberSkill(
        skill_id=skill_id,
        name=name or skill_id,
        description="",
        summary=summary,
        content=content,
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        category="custom",
        tags=[],
        skill_metadata={},
        depends_on=[],
        status=status,
        version=version,
    )
    session.add(skill)
    session.flush()
    return skill


def _seed_skill_version(
    session: Session,
    *,
    skill_id: str,
    version_number: int,
    content: str,
    summary: str,
    created_by: str = "@test",
) -> CaliberSkillVersion:
    row = CaliberSkillVersion(
        skill_version_id=f"SKV-{skill_id}-{version_number}",
        skill_id=skill_id,
        version_number=version_number,
        content=content,
        summary=summary,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return row


def _seed_skill_with_history(
    session: Session,
    *,
    skill_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    name: str | None = None,
    content: str = "Full instructions for the skill.",
    summary: str = "When to use this skill.",
    version: int = 1,
    status: str = "active",
) -> CaliberSkill:
    skill = _seed_skill(
        session,
        skill_id=skill_id,
        project_id=project_id,
        owner=owner,
        visibility=visibility,
        name=name,
        content=content,
        summary=summary,
        version=version,
        status=status,
    )
    _seed_skill_version(
        session, skill_id=skill_id, version_number=version, content=content, summary=summary
    )
    return skill


# -- resolve --------------------------------------------------------------


def test_resolve_loads_the_exact_version_and_its_content(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        owner="@caller",
        content="Instructions v1",
        summary="Summary v1",
        version=1,
    )
    project = db_session.get(CaliberProject, PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        project,
        {"resource_id": "SKL-1", "version_ref": "1"},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert isinstance(resolved, ResolvedSkillVersion)
    assert resolved.skill_id == "SKL-1"
    assert resolved.name == "SKL-1"
    assert resolved.version_number == 1
    assert resolved.content == "Instructions v1"
    assert resolved.summary == "Summary v1"
    assert resolved.provider_ref == "caliber-skill-version:/SKL-1/1"


def test_resolve_loads_an_older_pinned_version_not_just_the_current_head(
    db_session: Session,
) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        owner="@caller",
        content="Instructions v1",
        summary="Summary v1",
        version=1,
    )
    skill = db_session.get(CaliberSkill, "SKL-1")
    assert skill is not None
    skill.content = "Instructions v2"
    skill.summary = "Summary v2"
    skill.version = 2
    db_session.flush()
    _seed_skill_version(
        db_session,
        skill_id="SKL-1",
        version_number=2,
        content="Instructions v2",
        summary="Summary v2",
    )
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "SKL-1", "version_ref": "1"},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert resolved.version_number == 1
    assert resolved.content == "Instructions v1"
    assert resolved.summary == "Summary v1"


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    identity = _identity()
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping", identity)  # type: ignore[arg-type]


def test_resolve_rejects_a_non_integer_version(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="integer"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": "x", "version_ref": "not-a-number"}, _identity()
        )


def test_resolve_raises_when_the_skill_does_not_exist(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": "SKL-ghost", "version_ref": "1"}, _identity()
        )


def test_resolve_raises_when_the_version_is_not_in_history(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-1", project_id=PROJECT_ID, owner="@caller")
    with pytest.raises(WorkspaceReleaseAdapterError, match="no recorded version"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "SKL-1", "version_ref": "99"},
            _identity("@caller", active_project_id=PROJECT_ID),
        )


def test_resolve_refuses_a_skill_bound_to_a_different_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_skill_with_history(
        db_session, skill_id="SKL-other", project_id=OTHER_PROJECT_ID, owner="@owner"
    )
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(db_session, None, {"resource_id": "SKL-other", "version_ref": "1"}, caller)


def test_resolve_allows_a_project_scoped_skill_for_an_active_member(db_session: Session) -> None:
    """The positive side of the project-tier check: a caller who is an
    active member of the target's own project (not its owner) can still
    resolve it -- proving this reuses the *real* visibility model
    (owner-or-member), not a stricter "owner only" shortcut."""
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-team", project_id=PROJECT_ID, owner="@owner")
    db_session.add(
        CaliberProjectMember(
            member_id="PM-skill-adapter-1",
            project_id=PROJECT_ID,
            user_id="@teammate",
            role="editor",
            status="active",
            created_by="@owner",
        )
    )
    db_session.flush()
    member = _identity("@teammate", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "SKL-team", "version_ref": "1"}, member
    )
    assert resolved.skill_id == "SKL-team"


def test_resolve_refuses_another_users_unshared_personal_skill(db_session: Session) -> None:
    """The actual disclosure this design closes from the start: a personal
    skill (``project_id=None``, ``visibility="user"``) has the *same*
    ``None`` project id as a public skill, so a bare ``skill.project_id !=
    caller's project`` comparison could not tell them apart and would let
    any operator on any project snapshot another user's unshared personal
    skill by id. Must be refused, the same way
    ``routes/skills.py``'s lookup routes already refuse it."""
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-personal",
        project_id=None,
        owner="@someone-else",
        content="secret instructions",
    )
    other_user = _identity("@a-different-user", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": "SKL-personal", "version_ref": "1"}, other_user
        )


def test_resolve_allows_the_owners_own_personal_skill(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-mine", project_id=None, owner="@owner-self")
    owner = _identity("@owner-self", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "SKL-mine", "version_ref": "1"}, owner
    )
    assert resolved.skill_id == "SKL-mine"


def test_resolve_allows_a_public_skill_for_any_caller(db_session: Session) -> None:
    """A genuinely public skill also has ``project_id=None`` -- proving this
    allows that tier too, not just refusing everything with a ``None``
    project id."""
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-public",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    stranger = _identity("@total-stranger", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "SKL-public", "version_ref": "1"}, stranger
    )
    assert resolved.skill_id == "SKL-public"


def test_resolve_admin_bypasses_visibility(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session, skill_id="SKL-personal", project_id=None, owner="@someone-else"
    )
    admin = _identity("@platform-admin", active_project_id=PROJECT_ID, admin=True)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "SKL-personal", "version_ref": "1"}, admin
    )
    assert resolved.skill_id == "SKL-personal"


# -- snapshot ---------------------------------------------------------------


def test_snapshot_computes_a_genuine_content_digest() -> None:
    resolved = ResolvedSkillVersion(
        skill_id="SKL-1",
        name="reasoning-v2",
        version_number=3,
        content="Full instructions",
        summary="When to use it",
        provider_ref="caliber-skill-version:/SKL-1/3",
    )
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == "SKL-1"
    assert pin.version_ref == "3"
    expected_sha = _content_sha256("Full instructions", "When to use it")
    assert pin.content_sha256 == expected_sha
    assert pin.provider_ref == "caliber-skill-version:/SKL-1/3"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == SKILL_ADAPTER_VERSION
    assert pin.resolution["content_length"] == len("Full instructions")
    assert pin.resolution["summary_length"] == len("When to use it")


def test_snapshot_distinguishes_different_content() -> None:
    a = ADAPTER.snapshot(None, ResolvedSkillVersion("SKL-1", "s", 1, "content A", "summary", "ref"))
    b = ADAPTER.snapshot(None, ResolvedSkillVersion("SKL-1", "s", 1, "content B", "summary", "ref"))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_distinguishes_different_summaries() -> None:
    a = ADAPTER.snapshot(None, ResolvedSkillVersion("SKL-1", "s", 1, "content", "summary A", "ref"))
    b = ADAPTER.snapshot(None, ResolvedSkillVersion("SKL-1", "s", 1, "content", "summary B", "ref"))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved skill version"):
        ADAPTER.snapshot(None, {"not": "resolved"})


# -- validate -----------------------------------------------------------


def test_validate_accepts_an_unscoped_pin(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-validate-1", project_id=PROJECT_ID)
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-1",
        revision_id="WSR-validate-1",
        resource_type="skill",
        logical_name="SKL-validate-1",
        resource_id="SKL-validate-1",
        version_ref="1",
        content_sha256="a" * 64,
        provider_ref="caliber-skill-version:/SKL-validate-1/1",
        purpose="runtime",
        resolution={},
    )
    result = ADAPTER.validate(db_session, pin)
    assert result == {
        "valid": True,
        "resource_pin_id": "WSRR-validate-1",
        "skill_id": "SKL-validate-1",
        "version": 1,
    }


def test_validate_refuses_a_missing_skill(db_session: Session) -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-ghost",
        revision_id="WSR-validate-ghost",
        resource_type="skill",
        logical_name="SKL-ghost",
        resource_id="SKL-ghost",
        version_ref="1",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.validate(db_session, pin)


def test_validate_refuses_a_skill_outside_the_environments_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_skill_with_history(db_session, skill_id="SKL-other", project_id=OTHER_PROJECT_ID)
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-2",
        revision_id="WSR-validate-2",
        resource_type="skill",
        logical_name="SKL-other",
        resource_id="SKL-other",
        version_ref="1",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)


def test_validate_rejects_a_non_integer_version(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-validate-3", project_id=PROJECT_ID)
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-3",
        revision_id="WSR-validate-3",
        resource_type="skill",
        logical_name="SKL-validate-3",
        resource_id="SKL-validate-3",
        version_ref="not-a-number",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="non-integer"):
        ADAPTER.validate(db_session, pin)


# -- prepare_release ------------------------------------------------------


def _pin(
    *, version_ref: str = "1", content_sha256: str | None = None
) -> CaliberWorkspaceRevisionResource:
    return CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-prep-1",
        revision_id="WSR-prep-1",
        resource_type="skill",
        logical_name="SKL-1",
        resource_id="SKL-1",
        version_ref=version_ref,
        content_sha256=content_sha256 or ("a" * 64),
        purpose="runtime",
        resolution={},
    )


def _environment() -> CaliberWorkspaceEnvironment:
    return CaliberWorkspaceEnvironment(
        environment_id="WSE-prep-1",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )


def test_prepare_release_verifies_when_before_differs_from_after() -> None:
    prepared = ADAPTER.prepare_release(_pin(version_ref="2"), _environment(), before_ref="1")
    assert prepared.action == "verify"
    assert prepared.target_ref == "skill:SKL-1"
    assert prepared.before_ref == "1"
    assert prepared.after_ref == "2"
    assert prepared.metadata["skill_id"] == "SKL-1"
    assert prepared.metadata["content_sha256"] == "a" * 64


def test_prepare_release_is_a_no_op_when_before_equals_after() -> None:
    prepared = ADAPTER.prepare_release(_pin(version_ref="1"), _environment(), before_ref="1")
    assert prepared.action == "no_op"


# -- apply / observe / rollback -------------------------------------------


def _prepared_action(
    *,
    action: str = "verify",
    after_ref: str = "1",
    skill_id: str = "SKL-1",
    content_sha256: str | None = None,
) -> PreparedAction:
    return PreparedAction(
        resource_pin_id="WSRR-apply-1",
        resource_type="skill",
        action=action,
        target_ref=f"skill:{skill_id}",
        before_ref="0",
        after_ref=after_ref,
        metadata={
            "skill_id": skill_id,
            "environment_id": "WSE-1",
            "project_id": PROJECT_ID,
            "content_sha256": content_sha256 or _content_sha256("Instructions v1", "Summary v1"),
        },
    )


def test_apply_release_confirms_matching_content(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        content="Instructions v1",
        summary="Summary v1",
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == 1
    assert outcome.provider_result["content_sha256"] == _content_sha256(
        "Instructions v1", "Summary v1"
    )


def test_apply_release_no_op_does_not_touch_the_database(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(action="no_op"))
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == "1"


def test_apply_release_reports_a_missing_skill(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(skill_id="SKL-ghost"))
    assert outcome.status == "failed"
    assert outcome.error_code == "skill_not_found"


def test_apply_release_reports_an_archived_skill(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        content="Instructions v1",
        summary="Summary v1",
        status="archived",
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "skill_archived"


def test_apply_release_reports_a_project_mismatch(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_skill_with_history(db_session, skill_id="SKL-1", project_id=OTHER_PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "resource_outside_project"


def test_apply_release_reports_an_invalid_version(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-1", project_id=PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref="not-a-number"))
    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_skill_version"


def test_apply_release_reports_a_missing_history_row(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(db_session, skill_id="SKL-1", project_id=PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref="99"))
    assert outcome.status == "failed"
    assert outcome.error_code == "skill_version_not_found"


def test_apply_release_reports_content_drift(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        content="Instructions v1",
        summary="Summary v1",
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action(content_sha256="stale" + "a" * 59))
    assert outcome.status == "failed"
    assert outcome.error_code == "skill_content_drifted"


def test_rollback_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        content="Instructions v1",
        summary="Summary v1",
    )
    outcome = ADAPTER.rollback_release(db_session, _prepared_action())
    assert outcome.status == "applied"


def test_observe_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_skill_with_history(
        db_session,
        skill_id="SKL-1",
        project_id=PROJECT_ID,
        content="Instructions v1",
        summary="Summary v1",
    )
    outcome = ADAPTER.observe_release(db_session, _prepared_action())
    assert outcome.status == "applied"


def test_malformed_target_ref_is_rejected(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad",
        resource_type="skill",
        action="verify",
        target_ref="not-a-skill-ref",
        before_ref=None,
        after_ref="1",
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="malformed"):
        ADAPTER._verify(db_session, bad)


def test_apply_release_requires_an_after_ref(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad-2",
        resource_type="skill",
        action="verify",
        target_ref="skill:SKL-1",
        before_ref=None,
        after_ref=None,
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="no after_ref"):
        ADAPTER._verify(db_session, bad)
