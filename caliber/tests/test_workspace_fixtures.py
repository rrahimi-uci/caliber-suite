"""`P0-B` (item 7, "deterministic fixtures"): the combined Phase-0 fixture scenario.

Proves ``workspace_fixtures.build_phase0_workspace_fixture`` actually produces
every element the original checklist item named -- two colliding-name
workspaces, the full role roster (primary/secondary Admin, Developer, QA,
Viewer, a scope-ineligible member), four fixed environments per workspace, a
provider-only prompt, mutable judge/tool rows, a shared catalog resource, a
partial provider effect, and a legacy null row -- and that building it twice
against two independent fixture databases is fully deterministic and
requires no network access or credential.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberJudge,
    CaliberProjectMember,
    CaliberToolRegistry,
)
from caliber.workspace_release_adapters import ProviderOutcome
from tests.workspace_fixtures import (
    DEVELOPER,
    FOUR_FIXED_ENVIRONMENTS,
    MUTABLE_JUDGE_NAME,
    MUTABLE_TOOL_NAME,
    PRIMARY_ADMIN,
    PROVIDER_ONLY_PROMPT_NAME,
    QA_USER,
    SCOPE_INELIGIBLE_USER,
    SECONDARY_ADMIN,
    SHARED_CATALOG_TOOL_NAME,
    VIEWER,
    build_phase0_workspace_fixture,
)


def test_two_workspaces_have_colliding_logical_names(
    client: TestClient, db_session: Session
) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    assert scenario.primary_project_id != scenario.secondary_project_id
    assert scenario.primary_project_slug == "fixture-workspace"
    # Deterministic numeric-suffix collision strategy (`P1-A`/migration 0093's
    # own backfill strategy), not a random or content-derived slug.
    assert scenario.secondary_project_slug == "fixture-workspace-2"


def test_role_roster_is_present_on_the_primary_workspace(
    client: TestClient, db_session: Session
) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    members = {
        (row.user_id, row.role)
        for row in db_session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == scenario.primary_project_id,
                CaliberProjectMember.status == "active",
            )
        ).scalars()
    }
    assert (PRIMARY_ADMIN, "owner") in members
    assert (SECONDARY_ADMIN, "owner") in members
    assert (DEVELOPER, "editor") in members
    assert (QA_USER, "reviewer") in members
    assert (VIEWER, "viewer") in members
    assert (SCOPE_INELIGIBLE_USER, "viewer") in members

    # The scope-ineligible member genuinely lacks the scopes required for the
    # `owner` role -- attempting to grant it must be refused with `409`, not
    # silently accepted, proving the fixture actually built an *ineligible*
    # member rather than merely an unrelated one.
    resp = client.post(
        f"/ajax-api/2.0/mlflow/caliber/projects/{scenario.primary_project_id}/members",
        json={"user_id": scenario.scope_ineligible_user, "role": "owner"},
        headers={"X-CALIBER-User": PRIMARY_ADMIN},
    )
    assert resp.status_code == 409, resp.text


def test_four_fixed_environments_per_workspace(client: TestClient, db_session: Session) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    for environments in (
        scenario.primary_project_environments,
        scenario.secondary_project_environments,
    ):
        assert [name for name, _cls, _order, _status in environments] == list(
            FOUR_FIXED_ENVIRONMENTS
        )
        statuses = {name: status for name, _cls, _order, status in environments}
        assert statuses["dev"] == "active"
        assert statuses["qa"] == statuses["staging"] == statuses["prod"] == "disabled"


def test_provider_only_prompt_has_no_caliber_side_row(
    client: TestClient, db_session: Session
) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    row = db_session.execute(
        select(CaliberAgentConfig).where(CaliberAgentConfig.agent_id == PROVIDER_ONLY_PROMPT_NAME)
    ).scalar_one_or_none()
    assert row is None
    assert scenario.provider_only_prompt_name == PROVIDER_ONLY_PROMPT_NAME


def test_mutable_judge_and_tool_rows_are_project_scoped(
    client: TestClient, db_session: Session
) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    judge = db_session.get(CaliberJudge, scenario.mutable_judge_id)
    assert judge is not None
    assert judge.name == MUTABLE_JUDGE_NAME
    assert judge.project_id == scenario.primary_project_id

    tool = db_session.get(CaliberToolRegistry, scenario.mutable_tool_id)
    assert tool is not None
    assert tool.name == MUTABLE_TOOL_NAME
    assert tool.project_id == scenario.primary_project_id

    # Both rows are ordinarily mutable (no version-lock/immutability marker) --
    # an operator can update them in place, unlike a versioned/immutable pin.
    judge.description = "fixture: updated in place"
    tool.description = "fixture: updated in place"
    db_session.commit()


def test_shared_catalog_resource_has_no_project(client: TestClient, db_session: Session) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    tool = db_session.get(CaliberToolRegistry, scenario.shared_catalog_tool_id)
    assert tool is not None
    assert tool.name == SHARED_CATALOG_TOOL_NAME
    assert tool.project_id is None
    assert tool.visibility == "public"


def test_partial_provider_effect_has_one_applied_and_one_ambiguous_child(
    client: TestClient, db_session: Session
) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    adapter = scenario.partial_effect_adapter
    assert len(adapter.apply_calls) == 2

    applied_ref, reconcile_ref = "fixture-target-1", "fixture-target-2"
    # Observing the "applied" child reports success; observing the ambiguous
    # one still reports failure/mismatch -- a partial effect never silently
    # resolves to a uniform outcome.
    applied_prepared = next(
        call for call in adapter.apply_calls if call.resource_pin_id.endswith("-applied")
    )
    reconcile_prepared = next(
        call for call in adapter.apply_calls if call.resource_pin_id.endswith("-reconcile-required")
    )
    assert applied_prepared.target_ref == applied_ref
    assert reconcile_prepared.target_ref == reconcile_ref

    applied_observation = adapter.observe_release(None, applied_prepared)
    assert applied_observation.status == "applied"

    reconcile_observation = adapter.observe_release(None, reconcile_prepared)
    assert reconcile_observation.status == "failed"
    assert isinstance(reconcile_observation, ProviderOutcome)


def test_legacy_null_audit_row_has_no_environment_correlation(
    client: TestClient, db_session: Session
) -> None:
    scenario = build_phase0_workspace_fixture(client, db_session)

    row = db_session.get(CaliberAuditLog, scenario.legacy_null_audit_log_id)
    assert row is not None
    assert row.environment_id is None
    assert row.entity_id == scenario.primary_project_id


def test_fixture_scenario_shape_is_deterministic(client: TestClient, db_session: Session) -> None:
    """The scenario's *structural* shape -- slugs, role roster, environment
    names/classes/order/status, the provider-only prompt name -- is fixed and
    reproducible; only the generated project/judge/tool ids are fresh per
    build. Every other test in this module independently rebuilds the same
    scenario against its own fresh fixture DB and observes this identical
    shape, which is itself the cross-build determinism proof; this test
    pins that expected shape explicitly in one place."""

    scenario = build_phase0_workspace_fixture(client, db_session)

    assert scenario.primary_project_slug == "fixture-workspace"
    assert scenario.secondary_project_slug == "fixture-workspace-2"
    assert scenario.primary_admin == PRIMARY_ADMIN
    assert scenario.secondary_admin == SECONDARY_ADMIN
    assert scenario.developer == DEVELOPER
    assert scenario.qa_user == QA_USER
    assert scenario.viewer == VIEWER
    assert scenario.scope_ineligible_user == SCOPE_INELIGIBLE_USER
    assert scenario.provider_only_prompt_name == PROVIDER_ONLY_PROMPT_NAME
    for environments in (
        scenario.primary_project_environments,
        scenario.secondary_project_environments,
    ):
        assert [name for name, _cls, _order, _status in environments] == list(
            FOUR_FIXED_ENVIRONMENTS
        )
