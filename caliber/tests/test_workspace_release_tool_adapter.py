"""`P4-B`/`P4-C`: the third real Workspace resource adapter -- a CALIBER
registered tool.

Mirrors ``test_workspace_release_prompt_adapter.py``'s pattern and scenario
coverage (proving the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol against a genuine resource type, including the full cross-user/
cross-project visibility suite), adapted for a tool: unlike a prompt, a
``CaliberToolRegistry`` row is itself both the identity/visibility record and
the content source, so there is no MLflow (or any other external system) to
stub here -- every test is a plain, offline database fixture.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberToolRegistry,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workflows.manifest import canonical_json
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)
from caliber.workspace_release_tool_adapter import (
    TOOL_ADAPTER_VERSION,
    ResolvedToolVersion,
    ToolWorkspaceResourceAdapter,
    _row_content_sha256,
)

PROJECT_ID = "PRJ-tool-adapter"
OTHER_PROJECT_ID = "PRJ-tool-adapter-other"
ADAPTER = ToolWorkspaceResourceAdapter()


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
        CaliberProject(project_id=project_id, name=f"Tool adapter {project_id}", owner="admin")
    )
    session.flush()


def _seed_tool(
    session: Session,
    *,
    tool_id: str,
    name: str,
    version: str = "1",
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    status: str = "active",
    side_effect_level: str = "read",
    module_path: str = "caliber.tools.example",
    callable_name: str = "run",
    backend_config: dict | None = None,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    requires_approval: bool = False,
    allow_in_preview: bool = False,
    secret_refs: list[str] | None = None,
) -> CaliberToolRegistry:
    tool = CaliberToolRegistry(
        tool_id=tool_id,
        name=name,
        version=version,
        description="a test tool",
        module_path=module_path,
        callable_name=callable_name,
        execution_backend="python_callable",
        backend_config=backend_config,
        input_schema=input_schema,
        output_schema=output_schema,
        side_effect_level=side_effect_level,
        requires_approval=requires_approval,
        allow_in_preview=allow_in_preview,
        secret_refs=secret_refs or [],
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        status=status,
    )
    session.add(tool)
    session.flush()
    return tool


# -- resolve --------------------------------------------------------------


def test_resolve_loads_the_exact_version(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_tool(
        db_session,
        tool_id="TOOL-1",
        name="search-tool",
        version="2",
        project_id=PROJECT_ID,
        owner="@caller",
        backend_config={"timeout": 5},
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        secret_refs=["SEARCH_API_KEY"],
    )
    project = db_session.get(CaliberProject, PROJECT_ID)
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, project, {"resource_id": "search-tool", "version_ref": "2"}, caller
    )
    assert isinstance(resolved, ResolvedToolVersion)
    assert resolved.tool_id == "TOOL-1"
    assert resolved.name == "search-tool"
    assert resolved.version == "2"
    assert resolved.module_path == "caliber.tools.example"
    assert resolved.callable_name == "run"
    assert resolved.backend_config == {"timeout": 5}
    assert resolved.input_schema == {"type": "object"}
    assert resolved.output_schema == {"type": "object"}
    assert resolved.secret_refs == ("SEARCH_API_KEY",)
    assert resolved.status == "active"
    assert resolved.provider_ref == "caliber-tool-registry:/TOOL-1"


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    identity = _identity()
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x", "version_ref": "  "}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping", identity)  # type: ignore[arg-type]


def test_resolve_raises_when_the_tool_version_does_not_exist(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(db_session, None, {"resource_id": "ghost", "version_ref": "9"}, _identity())


def test_resolve_refuses_a_tool_bound_to_a_different_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_tool(
        db_session,
        tool_id="TOOL-2",
        name="owned-elsewhere",
        project_id=OTHER_PROJECT_ID,
        owner="@owner",
    )
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": "owned-elsewhere", "version_ref": "1"}, caller
        )


def test_resolve_allows_a_project_scoped_tool_for_an_active_member(db_session: Session) -> None:
    """The positive side of the project-tier check: a caller who is an
    active member of the target's own project (not its owner) can still
    resolve it -- proving this reuses the *real* visibility model
    (owner-or-member) rather than a stricter "owner only" shortcut."""
    _seed_project(db_session)
    _seed_tool(
        db_session, tool_id="TOOL-3", name="team-tool", project_id=PROJECT_ID, owner="@owner"
    )
    db_session.add(
        CaliberProjectMember(
            member_id="PM-tool-adapter-1",
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
        db_session, None, {"resource_id": "team-tool", "version_ref": "1"}, member
    )
    assert resolved.name == "team-tool"


def test_resolve_refuses_another_users_unshared_personal_tool(db_session: Session) -> None:
    """The actual disclosure this fix guards against from the start: a
    personal tool (``project_id=None``, ``visibility="user"``) has the
    *same* ``None`` project id as a public tool, so a bare
    ``row.project_id != caller's project`` comparison could not tell them
    apart and would let any caller snapshot another user's unshared personal
    tool by name -- must be refused, the same way ``routes/tools.py``'s own
    ``_visible_tool_or_404`` already refuses it for every tool route."""
    _seed_project(db_session)
    _seed_tool(
        db_session,
        tool_id="TOOL-4",
        name="my-personal-tool",
        project_id=None,
        owner="@someone-else",
    )
    other_user = _identity("@a-different-user", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session, None, {"resource_id": "my-personal-tool", "version_ref": "1"}, other_user
        )


def test_resolve_allows_the_owners_own_personal_tool(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_tool(
        db_session, tool_id="TOOL-5", name="my-personal-tool", project_id=None, owner="@owner-self"
    )
    owner = _identity("@owner-self", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "my-personal-tool", "version_ref": "1"}, owner
    )
    assert resolved.name == "my-personal-tool"


def test_resolve_allows_a_public_tool_for_any_caller(db_session: Session) -> None:
    """A genuinely public tool also has ``project_id=None`` -- proving the
    visibility filter allows this tier too, not just refusing everything
    with a ``None`` project id."""
    _seed_project(db_session)
    _seed_tool(
        db_session,
        tool_id="TOOL-6",
        name="shared-tool",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    stranger = _identity("@total-stranger", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "shared-tool", "version_ref": "1"}, stranger
    )
    assert resolved.name == "shared-tool"


def test_resolve_admin_bypasses_visibility(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_tool(
        db_session,
        tool_id="TOOL-7",
        name="my-personal-tool",
        project_id=None,
        owner="@someone-else",
    )
    admin = _identity("@platform-admin", active_project_id=PROJECT_ID, admin=True)
    resolved = ADAPTER.resolve(
        db_session, None, {"resource_id": "my-personal-tool", "version_ref": "1"}, admin
    )
    assert resolved.name == "my-personal-tool"


# -- snapshot ---------------------------------------------------------------


def _resolved(**overrides: object) -> ResolvedToolVersion:
    base = {
        "tool_id": "TOOL-x",
        "name": "search-tool",
        "version": "2",
        "module_path": "caliber.tools.example",
        "callable_name": "run",
        "execution_backend": "python_callable",
        "backend_config": None,
        "input_schema": None,
        "output_schema": None,
        "side_effect_level": "read",
        "requires_approval": False,
        "allow_in_preview": False,
        "secret_refs": (),
        "status": "active",
        "provider_ref": "caliber-tool-registry:/TOOL-x",
    }
    base.update(overrides)
    return ResolvedToolVersion(**base)  # type: ignore[arg-type]


def test_snapshot_computes_a_genuine_content_digest() -> None:
    resolved = _resolved()
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == "search-tool"
    assert pin.version_ref == "2"
    assert pin.provider_ref == "caliber-tool-registry:/TOOL-x"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == TOOL_ADAPTER_VERSION
    assert pin.resolution["tool_id"] == "TOOL-x"
    assert pin.resolution["status"] == "active"
    expected_payload = {
        "module_path": "caliber.tools.example",
        "callable_name": "run",
        "execution_backend": "python_callable",
        "backend_config": None,
        "input_schema": None,
        "output_schema": None,
        "side_effect_level": "read",
        "requires_approval": False,
        "allow_in_preview": False,
        "secret_refs": [],
    }
    assert (
        pin.content_sha256
        == hashlib.sha256(canonical_json(expected_payload).encode("utf-8")).hexdigest()
    )


def test_snapshot_distinguishes_different_definitions() -> None:
    a = ADAPTER.snapshot(None, _resolved(callable_name="run"))
    b = ADAPTER.snapshot(None, _resolved(callable_name="run_v2"))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_distinguishes_different_safety_policy() -> None:
    """Content includes safety-relevant flags, not just the executable
    reference -- flipping ``requires_approval`` changes what the tool means
    to run, so it must change the pin's digest too."""
    a = ADAPTER.snapshot(None, _resolved(requires_approval=False))
    b = ADAPTER.snapshot(None, _resolved(requires_approval=True))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved tool version"):
        ADAPTER.snapshot(None, {"not": "resolved"})


# -- validate -----------------------------------------------------------


def _pin(**overrides: object) -> CaliberWorkspaceRevisionResource:
    base = {
        "resource_pin_id": "WSRR-validate-1",
        "revision_id": "WSR-validate-1",
        "resource_type": "tool",
        "logical_name": "search-tool",
        "resource_id": "search-tool",
        "version_ref": "2",
        "content_sha256": "a" * 64,
        "provider_ref": "caliber-tool-registry:/TOOL-1",
        "purpose": "runtime",
        "resolution": {},
    }
    base.update(overrides)
    return CaliberWorkspaceRevisionResource(**base)  # type: ignore[arg-type]


def test_validate_accepts_a_matching_pin(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_tool(db_session, tool_id="TOOL-8", name="search-tool", version="2", project_id=PROJECT_ID)
    result = ADAPTER.validate(db_session, _pin())
    assert result == {
        "valid": True,
        "resource_pin_id": "WSRR-validate-1",
        "tool_name": "search-tool",
        "version": "2",
        "tool_id": "TOOL-8",
    }


def test_validate_raises_when_the_tool_version_is_missing(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.validate(db_session, _pin())


def test_validate_refuses_a_tool_outside_the_environments_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_tool(
        db_session,
        tool_id="TOOL-9",
        name="owned-elsewhere",
        version="1",
        project_id=OTHER_PROJECT_ID,
    )
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = _pin(resource_id="owned-elsewhere", logical_name="owned-elsewhere", version_ref="1")
    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)


# -- prepare_release ------------------------------------------------------


def _environment(**overrides: object) -> CaliberWorkspaceEnvironment:
    base = {
        "environment_id": "WSE-prep-1",
        "project_id": PROJECT_ID,
        "name": "prod",
        "environment_class": "production",
        "promotion_order": 40,
        "status": "active",
        "created_by": "admin",
    }
    base.update(overrides)
    return CaliberWorkspaceEnvironment(**base)  # type: ignore[arg-type]


def test_prepare_release_builds_a_verify_action() -> None:
    pin = _pin()
    environment = _environment()
    prepared = ADAPTER.prepare_release(pin, environment, before_ref="1")
    assert prepared.action == "verify"
    assert prepared.target_ref == "tool:search-tool@prod"
    assert prepared.before_ref == "1"
    assert prepared.after_ref == "2"
    assert prepared.metadata["tool_name"] == "search-tool"
    assert prepared.metadata["alias"] == "prod"
    assert prepared.metadata["content_sha256"] == "a" * 64


def test_prepare_release_is_a_no_op_when_before_equals_after() -> None:
    pin = _pin()
    environment = _environment()
    prepared = ADAPTER.prepare_release(pin, environment, before_ref="2")
    assert prepared.action == "no_op"


# -- apply / observe / rollback -------------------------------------------


def _prepared_action(
    *, action: str = "verify", after_ref: str = "2", **meta: object
) -> PreparedAction:
    metadata = {"tool_name": "search-tool", "alias": "prod", "content_sha256": "a" * 64}
    metadata.update(meta)
    return PreparedAction(
        resource_pin_id="WSRR-apply-1",
        resource_type="tool",
        action=action,
        target_ref="tool:search-tool@prod",
        before_ref="1",
        after_ref=after_ref,
        metadata=metadata,
    )


def test_apply_release_verifies_a_matching_live_tool(db_session: Session) -> None:
    _seed_project(db_session)
    tool = _seed_tool(
        db_session, tool_id="TOOL-10", name="search-tool", version="2", project_id=PROJECT_ID
    )
    live_sha256 = _row_content_sha256(tool)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(content_sha256=live_sha256))
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == "2"
    assert outcome.provider_result["content_sha256"] == live_sha256


def test_apply_release_no_op_does_not_touch_the_database(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(action="no_op"))
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == "2"


def test_apply_release_reports_a_missing_tool(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "tool_not_found"


def test_apply_release_reports_an_archived_tool(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_tool(
        db_session,
        tool_id="TOOL-11",
        name="search-tool",
        version="2",
        project_id=PROJECT_ID,
        status="archived",
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "tool_archived"


def test_apply_release_reports_content_drift(db_session: Session) -> None:
    """The core safety property this adapter adds for a resource type whose
    row remains mutable after registration: a release must refuse to
    "verify" content that no longer matches what was actually pinned."""
    _seed_project(db_session)
    _seed_tool(
        db_session,
        tool_id="TOOL-12",
        name="search-tool",
        version="2",
        project_id=PROJECT_ID,
        side_effect_level="read",
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action(content_sha256="b" * 64))
    assert outcome.status == "failed"
    assert outcome.error_code == "tool_content_drifted"


def test_apply_release_reports_missing_after_ref(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="no after_ref"):
        ADAPTER.apply_release(db_session, _prepared_action(after_ref=""))


def test_rollback_release_verifies_the_same_way(db_session: Session) -> None:
    _seed_project(db_session)
    tool = _seed_tool(
        db_session, tool_id="TOOL-13", name="search-tool", version="2", project_id=PROJECT_ID
    )
    live_sha256 = _row_content_sha256(tool)
    outcome = ADAPTER.rollback_release(db_session, _prepared_action(content_sha256=live_sha256))
    assert outcome.status == "applied"


def test_observe_release_verifies_the_same_way(db_session: Session) -> None:
    _seed_project(db_session)
    tool = _seed_tool(
        db_session, tool_id="TOOL-14", name="search-tool", version="2", project_id=PROJECT_ID
    )
    live_sha256 = _row_content_sha256(tool)
    outcome = ADAPTER.observe_release(db_session, _prepared_action(content_sha256=live_sha256))
    assert outcome.status == "applied"


def test_observe_release_reports_a_missing_tool(db_session: Session) -> None:
    outcome = ADAPTER.observe_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "tool_not_found"


@pytest.mark.parametrize("target_ref", ["not-a-tool-ref", "tool:@prod", "tool:search-tool@"])
def test_malformed_target_ref_is_rejected(db_session: Session, target_ref: str) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad",
        resource_type="tool",
        action="verify",
        target_ref=target_ref,
        before_ref=None,
        after_ref="1",
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="malformed"):
        ADAPTER.apply_release(db_session, bad)
