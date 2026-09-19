"""Phase 0 item 7: deterministic Workspace fixture scenario.

Original Phase-0 checklist text (``docs/workspace-plan.md``, "Phase 0 --
contract freeze and inventory", item 7)::

    Create deterministic fixtures: two workspaces with colliding logical
    names, a primary and secondary Admin, Developer, QA, Viewer,
    scope-ineligible user, four fixed environments, a provider-only
    prompt, mutable judge/tool rows, a shared catalog resource, a partial
    provider effect, and legacy null rows.

This module is the reusable, offline, deterministic scenario builder that
item asked for -- until now every test that needed one or two of these
elements (colliding project-name slugs, a scope-ineligible member, a
provider-only/bare prompt, a shared no-project catalog tool, ...) built its
own ad hoc, non-reusable version of it (see ``test_projects.py``,
``test_root_creation_scoping.py``, ``test_routes_prompts.py``), and no
single fixture produced the *combined* scenario the checklist named.

It composes only already-implemented, already-tested public route/service
surfaces (project creation, membership, tool/judge registration, the
:class:`~caliber.workspace_release_adapters.FakeWorkspaceResourceAdapter`)
plus one direct legacy-row construction for a state no live API path
produces anymore (a pre-`P1-A` audit row with no environment correlation --
``caliber.audit.record``'s own ``environment_id`` parameter still defaults
to ``None`` for exactly this reason). Nothing here adds production code or
behavior; it is test-fixture composition only, and it makes no network call
and needs no credential -- every assertion downstream of it can run fully
offline, matching this ticket's own acceptance bar ("Contract fixtures
execute offline").
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.audit import record as audit_record
from caliber.db.models import CaliberWorkspaceEnvironment
from caliber.workspace_release_adapters import (
    FakeWorkspaceResourceAdapter,
    PreparedAction,
    ProviderOutcome,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
TOOLS_PATH = f"{PREFIX}/tools"
JUDGES_PATH = f"{PREFIX}/judges"

#: Two names that slugify to the same prefix ("fixture-workspace"), so the
#: second workspace's slug collides with the first's and must fall back to
#: `P1-A`'s deterministic numeric-suffix strategy -- the concrete, already-
#: implemented behavior "two workspaces with colliding logical names" names.
PRIMARY_WORKSPACE_NAME = "Fixture Workspace"
SECONDARY_WORKSPACE_NAME = "Fixture Workspace!!"

PRIMARY_ADMIN = "@fixture-admin-primary"
SECONDARY_ADMIN = "@fixture-admin-secondary"
DEVELOPER = "@fixture-developer"
QA_USER = "@fixture-qa"
VIEWER = "@fixture-viewer"
SCOPE_INELIGIBLE_USER = "@fixture-scope-ineligible"

#: A prompt name with deliberately no `CaliberAgentConfig` hidden-target row
#: bound to it anywhere -- a "bare provider-only/legacy prompt" in exactly
#: the sense `test_routes_prompts.py`'s own `bare-prompt` fixture already
#: exercises (a prompt that exists only in the provider registry, with no
#: CALIBER-side binding to hide it behind or assign it to a project).
PROVIDER_ONLY_PROMPT_NAME = "fixture-provider-only-prompt"

MUTABLE_JUDGE_NAME = "fixture-mutable-judge"
MUTABLE_TOOL_NAME = "fixture-mutable-tool"
SHARED_CATALOG_TOOL_NAME = "fixture-shared-catalog-tool"

FOUR_FIXED_ENVIRONMENTS = ("dev", "qa", "staging", "prod")


@dataclass(frozen=True)
class Phase0WorkspaceFixture:
    """Every named element of Phase-0 checklist item 7, in one deterministic bundle."""

    # Two workspaces with colliding logical names.
    primary_project_id: str
    secondary_project_id: str
    primary_project_slug: str
    secondary_project_slug: str

    # Primary/secondary Admin, Developer, QA, Viewer, scope-ineligible user.
    primary_admin: str
    secondary_admin: str
    developer: str
    qa_user: str
    viewer: str
    scope_ineligible_user: str

    # Four fixed environments (name, environment_class, promotion_order, status).
    primary_project_environments: tuple[tuple[str, str, int, str], ...]
    secondary_project_environments: tuple[tuple[str, str, int, str], ...]

    # A provider-only prompt (no CALIBER-side row at all -- see the module docstring).
    provider_only_prompt_name: str

    # Mutable judge/tool rows, scoped into the primary workspace.
    mutable_judge_id: str
    mutable_tool_id: str

    # A shared catalog resource: a globally-named tool with no project (project_id IS
    # NULL), the same "shared fleet-wide handle" convention `P2-A` ratified for
    # `uq_tool_name_version`/`uq_judge_name`/etc.
    shared_catalog_tool_id: str

    # A partial provider effect: one child resource pin genuinely applied, a sibling
    # left ambiguous (`reconcile_required`) -- the exact shape `workspace_release_
    # operations.py` must never silently report as a uniform success or failure.
    partial_effect_adapter: FakeWorkspaceResourceAdapter
    partial_effect_applied_pin_id: str
    partial_effect_reconcile_required_pin_id: str

    # A legacy null row: an audit-log entry with no `environment_id` correlation,
    # the shape every pre-`P1-A` row has and `audit.record`'s own default still
    # produces for any caller that does not pass one.
    legacy_null_audit_log_id: int


def _grant_owner_role_eligibility(client: TestClient, *user_ids: str) -> None:
    """Grant every ``user_id`` full platform-Admin scope (``caliber.admin``,
    which implies ``caliber.operator``/``caliber.approver``/``caliber.viewer``
    -- ``auth.py``'s own ``_SCOPE_IMPLIES`` mapping).

    Mirrors ``test_projects.py::_make_eligible``'s config-mutation pattern
    (already proven there to make a user eligible for `project.create` and
    the Admin (`owner`) project role), extended to ``admin_users`` so the
    same identities can also register a tool (`register_tool` requires
    ``caliber.admin`` specifically, a stricter requirement than judge/
    project-membership routes).
    """

    existing = client.app.state.config.admin_users
    merged = ",".join(filter(None, [existing, *user_ids]))
    client.app.state.config = client.app.state.config.model_copy(update={"admin_users": merged})


def _create_project(client: TestClient, name: str, *, actor: str) -> str:
    resp = client.post(
        f"{PREFIX}/projects",
        json={"name": name, "description": "Phase 0 item 7 deterministic fixture"},
        headers={"X-CALIBER-User": actor},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["project_id"])


def _project_slug(db_session: Session, project_id: str) -> str:
    from caliber.db.models import CaliberProject

    return str(
        db_session.execute(
            select(CaliberProject.slug).where(CaliberProject.project_id == project_id)
        ).scalar_one()
    )


def _environment_rows(
    db_session: Session, project_id: str
) -> tuple[tuple[str, str, int, str], ...]:
    rows = (
        db_session.execute(
            select(CaliberWorkspaceEnvironment)
            .where(CaliberWorkspaceEnvironment.project_id == project_id)
            .order_by(CaliberWorkspaceEnvironment.promotion_order)
        )
        .scalars()
        .all()
    )
    return tuple((row.name, row.environment_class, row.promotion_order, row.status) for row in rows)


def _add_member(
    client: TestClient, project_id: str, user_id: str, role: str, *, actor: str
) -> None:
    resp = client.post(
        f"{PREFIX}/projects/{project_id}/members",
        json={"user_id": user_id, "role": role},
        headers={"X-CALIBER-User": actor},
    )
    assert resp.status_code == 201, resp.text


def _create_judge(
    client: TestClient, *, name: str, project_id: str | None, actor: str | None = None
) -> str:
    headers: dict[str, str] = {}
    if project_id:
        headers["X-CALIBER-Project"] = project_id
    if actor:
        headers["X-CALIBER-User"] = actor
    resp = client.post(
        JUDGES_PATH,
        json={"name": name, "instructions": "Score the response given {{ outputs }}."},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["judge_id"])


def _create_tool(
    client: TestClient, *, name: str, project_id: str | None, actor: str | None = None
) -> str:
    headers: dict[str, str] = {}
    if project_id:
        headers["X-CALIBER-Project"] = project_id
    if actor:
        headers["X-CALIBER-User"] = actor
    resp = client.post(
        TOOLS_PATH,
        json={
            "name": name,
            "version": "1.0.0",
            "module_path": "caliber.tools.demo",
            "callable_name": "run",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["tool_id"])


def _build_partial_provider_effect(
    adapter: FakeWorkspaceResourceAdapter,
) -> tuple[str, str]:
    """Apply two children through ``adapter``: one succeeds, one is ambiguous.

    Returns the ``(applied_pin_id, reconcile_required_pin_id)`` pair so callers
    can assert against them without re-deriving the fixture's own IDs.
    """

    applied_pin_id = "fixture-resource-pin-applied"
    reconcile_pin_id = "fixture-resource-pin-reconcile-required"

    adapter.queue_apply(ProviderOutcome("applied", "fixture-provider-op-1"))
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "fixture-provider-op-2",
            error_code="fixture_partial_effect",
            error_summary="fixture: provider outcome for the second child is ambiguous",
        )
    )

    applied = PreparedAction(
        resource_pin_id=applied_pin_id,
        resource_type=adapter.resource_type,
        action="promote",
        target_ref="fixture-target-1",
        before_ref=None,
        after_ref="fixture-target-1@v2",
    )
    reconcile_required = PreparedAction(
        resource_pin_id=reconcile_pin_id,
        resource_type=adapter.resource_type,
        action="promote",
        target_ref="fixture-target-2",
        before_ref=None,
        after_ref="fixture-target-2@v2",
    )

    adapter.apply_release(None, applied)
    adapter.apply_release(None, reconcile_required)

    return applied_pin_id, reconcile_pin_id


def build_phase0_workspace_fixture(
    client: TestClient, db_session: Session
) -> Phase0WorkspaceFixture:
    """Build the complete, deterministic Phase-0 item-7 fixture scenario.

    Fully offline: every step is either an in-process route call against the
    fixture ``TestClient``/DB, or a direct in-memory dataclass construction
    (the `FakeWorkspaceResourceAdapter`). No network call, credential, or
    external provider is ever touched.
    """

    _grant_owner_role_eligibility(client, PRIMARY_ADMIN, SECONDARY_ADMIN)

    # Two workspaces with colliding logical names, each with its own Admin.
    primary_project_id = _create_project(client, PRIMARY_WORKSPACE_NAME, actor=PRIMARY_ADMIN)
    secondary_project_id = _create_project(client, SECONDARY_WORKSPACE_NAME, actor=SECONDARY_ADMIN)

    # `P1-C`: multiple active `owner`-role memberships are allowed -- the
    # primary workspace gets both a primary and a secondary Admin.
    _add_member(client, primary_project_id, SECONDARY_ADMIN, "owner", actor=PRIMARY_ADMIN)
    _add_member(client, primary_project_id, DEVELOPER, "editor", actor=PRIMARY_ADMIN)
    _add_member(client, primary_project_id, QA_USER, "reviewer", actor=PRIMARY_ADMIN)
    _add_member(client, primary_project_id, VIEWER, "viewer", actor=PRIMARY_ADMIN)
    # A member present on the project but deliberately *not* granted
    # `caliber.operator`/`caliber.approver` -- any later attempt to escalate
    # them to the `owner` role must be refused (`_require_owner_role_eligible`).
    _add_member(client, primary_project_id, SCOPE_INELIGIBLE_USER, "viewer", actor=PRIMARY_ADMIN)

    mutable_judge_id = _create_judge(
        client, name=MUTABLE_JUDGE_NAME, project_id=primary_project_id, actor=PRIMARY_ADMIN
    )
    mutable_tool_id = _create_tool(
        client, name=MUTABLE_TOOL_NAME, project_id=primary_project_id, actor=PRIMARY_ADMIN
    )

    # A shared catalog resource: no active project -> global (`project_id IS
    # NULL`) public-catalog tool, the same "shared fleet-wide handle" `P2-A`
    # already ratified for this table's global `uq_tool_name_version`.
    shared_catalog_tool_id = _create_tool(client, name=SHARED_CATALOG_TOOL_NAME, project_id=None)

    adapter = FakeWorkspaceResourceAdapter(resource_type="fixture")
    applied_pin_id, reconcile_pin_id = _build_partial_provider_effect(adapter)

    legacy_log = audit_record(
        db_session,
        actor=PRIMARY_ADMIN,
        action="legacy_fixture_event",
        entity_type="project",
        entity_id=primary_project_id,
        # No `environment_id` -- the pre-`P1-A` legacy shape.
    )
    db_session.commit()

    return Phase0WorkspaceFixture(
        primary_project_id=primary_project_id,
        secondary_project_id=secondary_project_id,
        primary_project_slug=_project_slug(db_session, primary_project_id),
        secondary_project_slug=_project_slug(db_session, secondary_project_id),
        primary_admin=PRIMARY_ADMIN,
        secondary_admin=SECONDARY_ADMIN,
        developer=DEVELOPER,
        qa_user=QA_USER,
        viewer=VIEWER,
        scope_ineligible_user=SCOPE_INELIGIBLE_USER,
        primary_project_environments=_environment_rows(db_session, primary_project_id),
        secondary_project_environments=_environment_rows(db_session, secondary_project_id),
        provider_only_prompt_name=PROVIDER_ONLY_PROMPT_NAME,
        mutable_judge_id=mutable_judge_id,
        mutable_tool_id=mutable_tool_id,
        shared_catalog_tool_id=shared_catalog_tool_id,
        partial_effect_adapter=adapter,
        partial_effect_applied_pin_id=applied_pin_id,
        partial_effect_reconcile_required_pin_id=reconcile_pin_id,
        legacy_null_audit_log_id=legacy_log.log_id,
    )


__all__ = [
    "DEVELOPER",
    "FOUR_FIXED_ENVIRONMENTS",
    "MUTABLE_JUDGE_NAME",
    "MUTABLE_TOOL_NAME",
    "PRIMARY_ADMIN",
    "PRIMARY_WORKSPACE_NAME",
    "PROVIDER_ONLY_PROMPT_NAME",
    "QA_USER",
    "SCOPE_INELIGIBLE_USER",
    "SECONDARY_ADMIN",
    "SECONDARY_WORKSPACE_NAME",
    "SHARED_CATALOG_TOOL_NAME",
    "VIEWER",
    "Phase0WorkspaceFixture",
    "build_phase0_workspace_fixture",
]
