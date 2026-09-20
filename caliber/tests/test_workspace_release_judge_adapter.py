"""`P4-B`/`P4-C`: the third real Workspace resource adapter -- a CALIBER judge.

Mirrors ``test_workspace_release_prompt_adapter.py``'s pattern (proving the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol against a genuine resource type), but for a resource with no
version history at all -- see ``workspace_release_judge_adapter.py``'s module
docstring for why ``version_ref`` is the sentinel ``"current"`` on the way in
and a content digest on the way out.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import (
    CaliberJudge,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)
from caliber.workspace_release_judge_adapter import (
    CURRENT_VERSION_REF,
    JUDGE_ADAPTER_VERSION,
    JudgeWorkspaceResourceAdapter,
    ResolvedJudge,
    _content_sha256,
)

PROJECT_ID = "PRJ-judge-adapter"
OTHER_PROJECT_ID = "PRJ-judge-adapter-other"
ADAPTER = JudgeWorkspaceResourceAdapter()


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
        CaliberProject(project_id=project_id, name=f"Judge adapter {project_id}", owner="admin")
    )
    session.flush()


def _seed_judge(
    session: Session,
    *,
    judge_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    name: str | None = None,
    instructions: str = "Grade the {{ outputs }} against {{ expectations }}.",
    model: str | None = "gpt-5.6-luna",
    feedback_value_type: str | None = "bool",
) -> CaliberJudge:
    judge = CaliberJudge(
        judge_id=judge_id,
        name=name or judge_id,
        description="",
        instructions=instructions,
        model=model,
        feedback_value_type=feedback_value_type,
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        tags=[],
        status="active",
    )
    session.add(judge)
    session.flush()
    return judge


# -- resolve --------------------------------------------------------------


def test_resolve_loads_the_judge_and_returns_its_grading_fields(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_judge(db_session, judge_id="JDG-1", project_id=PROJECT_ID, owner="@caller")
    project = db_session.get(CaliberProject, PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        project,
        {"resource_id": "JDG-1", "version_ref": CURRENT_VERSION_REF},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert isinstance(resolved, ResolvedJudge)
    assert resolved.judge_id == "JDG-1"
    assert resolved.name == "JDG-1"
    assert resolved.instructions == "Grade the {{ outputs }} against {{ expectations }}."
    assert resolved.model == "gpt-5.6-luna"
    assert resolved.feedback_value_type == "bool"


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    identity = _identity()
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping", identity)  # type: ignore[arg-type]


def test_resolve_rejects_a_version_ref_other_than_current(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_judge(db_session, judge_id="JDG-1", project_id=PROJECT_ID, owner="@caller")
    with pytest.raises(WorkspaceReleaseAdapterError, match="must be 'current'"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "JDG-1", "version_ref": "3"},
            _identity("@caller", active_project_id=PROJECT_ID),
        )


def test_resolve_raises_when_the_judge_does_not_exist(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "JDG-ghost", "version_ref": CURRENT_VERSION_REF},
            _identity(),
        )


def test_resolve_refuses_a_judge_bound_to_a_different_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_judge(db_session, judge_id="JDG-other", project_id=OTHER_PROJECT_ID, owner="@owner")
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "JDG-other", "version_ref": CURRENT_VERSION_REF},
            caller,
        )


def test_resolve_allows_a_project_scoped_judge_for_an_active_member(db_session: Session) -> None:
    """The positive side of the project-tier check: a caller who is an
    active member of the target's own project (not its owner) can still
    resolve it -- proving this reuses the *real* visibility model
    (owner-or-member), not a stricter "owner only" shortcut."""
    _seed_project(db_session)
    _seed_judge(db_session, judge_id="JDG-team", project_id=PROJECT_ID, owner="@owner")
    db_session.add(
        CaliberProjectMember(
            member_id="PM-judge-adapter-1",
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
        db_session,
        None,
        {"resource_id": "JDG-team", "version_ref": CURRENT_VERSION_REF},
        member,
    )
    assert resolved.judge_id == "JDG-team"


def test_resolve_refuses_another_users_unshared_personal_judge(db_session: Session) -> None:
    """The actual disclosure this design closes from the start: a personal
    judge (``project_id=None``, ``visibility="user"``) has the *same*
    ``None`` project id as a public judge, so a bare ``judge.project_id !=
    caller's project`` comparison could not tell them apart and would let
    any operator on any project snapshot another user's unshared personal
    judge by id. Must be refused, the same way ``routes/judges.py``'s lookup
    routes already refuse it."""
    _seed_project(db_session)
    _seed_judge(
        db_session,
        judge_id="JDG-personal",
        project_id=None,
        owner="@someone-else",
        instructions="secret grading instructions",
    )
    other_user = _identity("@a-different-user", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "JDG-personal", "version_ref": CURRENT_VERSION_REF},
            other_user,
        )


def test_resolve_allows_the_owners_own_personal_judge(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_judge(db_session, judge_id="JDG-mine", project_id=None, owner="@owner-self")
    owner = _identity("@owner-self", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "JDG-mine", "version_ref": CURRENT_VERSION_REF},
        owner,
    )
    assert resolved.judge_id == "JDG-mine"


def test_resolve_allows_a_public_judge_for_any_caller(db_session: Session) -> None:
    """A genuinely public judge also has ``project_id=None`` -- proving this
    allows that tier too, not just refusing everything with a ``None``
    project id."""
    _seed_project(db_session)
    _seed_judge(
        db_session,
        judge_id="JDG-public",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    stranger = _identity("@total-stranger", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "JDG-public", "version_ref": CURRENT_VERSION_REF},
        stranger,
    )
    assert resolved.judge_id == "JDG-public"


def test_resolve_admin_bypasses_visibility(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_judge(db_session, judge_id="JDG-personal", project_id=None, owner="@someone-else")
    admin = _identity("@platform-admin", active_project_id=PROJECT_ID, admin=True)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "JDG-personal", "version_ref": CURRENT_VERSION_REF},
        admin,
    )
    assert resolved.judge_id == "JDG-personal"


# -- snapshot ---------------------------------------------------------------


def test_snapshot_computes_a_genuine_content_digest() -> None:
    resolved = ResolvedJudge(
        judge_id="JDG-1",
        name="quality-judge",
        instructions="Grade {{ outputs }}",
        model="gpt-5.6-luna",
        feedback_value_type="bool",
    )
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == "JDG-1"
    expected_sha = _content_sha256("quality-judge", "Grade {{ outputs }}", "gpt-5.6-luna", "bool")
    assert pin.version_ref == expected_sha
    assert pin.content_sha256 == expected_sha
    assert pin.provider_ref == "judge:JDG-1"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == JUDGE_ADAPTER_VERSION
    assert pin.resolution["instructions_length"] == len("Grade {{ outputs }}")


def test_snapshot_distinguishes_different_instructions() -> None:
    a = ADAPTER.snapshot(
        None, ResolvedJudge("JDG-1", "j", "instructions A", "gpt-5.6-luna", "bool")
    )
    b = ADAPTER.snapshot(
        None, ResolvedJudge("JDG-1", "j", "instructions B", "gpt-5.6-luna", "bool")
    )
    assert a.content_sha256 != b.content_sha256
    assert a.version_ref != b.version_ref


def test_snapshot_distinguishes_different_models_and_value_types() -> None:
    base = ADAPTER.snapshot(None, ResolvedJudge("JDG-1", "j", "grade it", "model-a", "bool"))
    other_model = ADAPTER.snapshot(None, ResolvedJudge("JDG-1", "j", "grade it", "model-b", "bool"))
    other_value_type = ADAPTER.snapshot(
        None, ResolvedJudge("JDG-1", "j", "grade it", "model-a", "float")
    )
    assert base.content_sha256 != other_model.content_sha256
    assert base.content_sha256 != other_value_type.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved judge"):
        ADAPTER.snapshot(None, {"not": "resolved"})


# -- validate -----------------------------------------------------------


def test_validate_accepts_an_unscoped_pin(db_session: Session) -> None:
    _seed_project(db_session)
    judge = _seed_judge(db_session, judge_id="JDG-validate-1", project_id=PROJECT_ID)
    content_sha = _content_sha256(
        judge.name, judge.instructions, judge.model, judge.feedback_value_type
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-1",
        revision_id="WSR-validate-1",
        resource_type="judge",
        logical_name="JDG-validate-1",
        resource_id="JDG-validate-1",
        version_ref=content_sha,
        content_sha256=content_sha,
        provider_ref="judge:JDG-validate-1",
        purpose="runtime",
        resolution={},
    )
    result = ADAPTER.validate(db_session, pin)
    assert result == {
        "valid": True,
        "resource_pin_id": "WSRR-validate-1",
        "judge_id": "JDG-validate-1",
        "content_sha256": content_sha,
    }


def test_validate_refuses_a_missing_judge(db_session: Session) -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-ghost",
        revision_id="WSR-validate-ghost",
        resource_type="judge",
        logical_name="JDG-ghost",
        resource_id="JDG-ghost",
        version_ref="a" * 64,
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.validate(db_session, pin)


def test_validate_refuses_a_judge_outside_the_environments_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_judge(db_session, judge_id="JDG-other", project_id=OTHER_PROJECT_ID)
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
        resource_type="judge",
        logical_name="JDG-other",
        resource_id="JDG-other",
        version_ref="a" * 64,
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)


# -- prepare_release ------------------------------------------------------


def _pin(*, version_ref: str = "a" * 64) -> CaliberWorkspaceRevisionResource:
    return CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-prep-1",
        revision_id="WSR-prep-1",
        resource_type="judge",
        logical_name="JDG-1",
        resource_id="JDG-1",
        version_ref=version_ref,
        content_sha256=version_ref,
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
    prepared = ADAPTER.prepare_release(
        _pin(version_ref="b" * 64), _environment(), before_ref="a" * 64
    )
    assert prepared.action == "verify"
    assert prepared.target_ref == "judge:JDG-1"
    assert prepared.before_ref == "a" * 64
    assert prepared.after_ref == "b" * 64
    assert prepared.metadata["judge_id"] == "JDG-1"


def test_prepare_release_is_a_no_op_when_before_equals_after() -> None:
    prepared = ADAPTER.prepare_release(
        _pin(version_ref="a" * 64), _environment(), before_ref="a" * 64
    )
    assert prepared.action == "no_op"


# -- apply / observe / rollback -------------------------------------------


def _prepared_action(
    *, action: str = "verify", after_ref: str = "a" * 64, judge_id: str = "JDG-1"
) -> PreparedAction:
    return PreparedAction(
        resource_pin_id="WSRR-apply-1",
        resource_type="judge",
        action=action,
        target_ref=f"judge:{judge_id}",
        before_ref="b" * 64,
        after_ref=after_ref,
        metadata={"judge_id": judge_id, "environment_id": "WSE-1", "project_id": PROJECT_ID},
    )


def test_apply_release_confirms_matching_content(db_session: Session) -> None:
    _seed_project(db_session)
    judge = _seed_judge(db_session, judge_id="JDG-1", project_id=PROJECT_ID)
    content_sha = _content_sha256(
        judge.name, judge.instructions, judge.model, judge.feedback_value_type
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "applied"
    assert outcome.provider_result["content_sha256"] == content_sha


def test_apply_release_no_op_does_not_touch_the_database(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(action="no_op"))
    assert outcome.status == "applied"
    assert outcome.provider_result["content_sha256"] == "a" * 64


def test_apply_release_reports_a_missing_judge(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(judge_id="JDG-ghost"))
    assert outcome.status == "failed"
    assert outcome.error_code == "judge_not_found"


def test_apply_release_reports_content_drift(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_judge(db_session, judge_id="JDG-1", project_id=PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref="stale" + "a" * 59))
    assert outcome.status == "failed"
    assert outcome.error_code == "judge_content_drifted"


def test_apply_release_reports_a_project_mismatch(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_judge(db_session, judge_id="JDG-1", project_id=OTHER_PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "resource_outside_project"


def test_rollback_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    judge = _seed_judge(db_session, judge_id="JDG-1", project_id=PROJECT_ID)
    content_sha = _content_sha256(
        judge.name, judge.instructions, judge.model, judge.feedback_value_type
    )
    outcome = ADAPTER.rollback_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "applied"


def test_observe_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    judge = _seed_judge(db_session, judge_id="JDG-1", project_id=PROJECT_ID)
    content_sha = _content_sha256(
        judge.name, judge.instructions, judge.model, judge.feedback_value_type
    )
    outcome = ADAPTER.observe_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "applied"


def test_malformed_target_ref_is_rejected(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad",
        resource_type="judge",
        action="verify",
        target_ref="not-a-judge-ref",
        before_ref=None,
        after_ref="a" * 64,
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="malformed"):
        ADAPTER._verify(db_session, bad)


def test_apply_release_requires_an_after_ref(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad-2",
        resource_type="judge",
        action="verify",
        target_ref="judge:JDG-1",
        before_ref=None,
        after_ref=None,
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="no after_ref"):
        ADAPTER._verify(db_session, bad)
