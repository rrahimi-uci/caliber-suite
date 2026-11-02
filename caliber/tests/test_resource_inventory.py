"""Tests for the machine-readable per-model project-scoping inventory
(`P0-A`).

Unlike `test_route_scope_inventory.py`/`test_worker_inventory.py`, nothing
here is hand-maintained -- every fact is safely, mechanically derivable, so
these tests check derivation correctness and one genuinely load-bearing
safety invariant, not completeness against a registry.
"""

from __future__ import annotations

from collections import Counter

from caliber.db.resource_inventory import (
    SCOPING_OWNED_CATALOG,
    SCOPING_PROJECT_ONLY,
    SCOPING_UNSCOPED,
    SCOPING_VISIBILITY,
    resource_inventory,
)
from caliber.db.scoping import owner_column

#: Today's exact per-category split, confirmed by direct introspection
#: against the live model registry. A ratchet, not a law: a new model
#: changing these numbers is expected over time. The point of pinning them
#: is that the change gets *noticed and looked at* (was this model's
#: scoping tier a deliberate choice?), the same spirit as
#: test_async_offload_ratchet.py's baseline.
_EXPECTED_COUNTS = {
    # +6 (`P4-D`): change-request heads, comments, reviewers, checks, reviews,
    # and external attestations are subordinate audit/review rows. Their
    # parent change request supplies the project authorization boundary.
    # +1 (`P4-E`): source-provider delivery inbox rows are subordinate to the
    # source binding and use source_id rather than a duplicate project_id.
    # +3 (`P5-A`): release evidence, decisions, and operation items are
    # subordinate immutable rows. Their release/operation parent supplies the
    # project authorization boundary, just as source-provider delivery rows
    # and change-request audit rows do.
    # -1 (`P2-R`): CaliberReleaseOperation gained a direct, nullable
    # project_id (backfilled from the released prompt's hidden
    # CaliberAgentConfig target) and moves to project_only -- see below.
    # -1 (`P2-Q`): CaliberVerificationItem gained a direct, nullable
    # project_id (backfilled from its own agent_id's CaliberAgentConfig
    # target) and moves to project_only -- see below.
    # +1 (`B02.1a`): CaliberRunManifest is write-once system state keyed by
    # workflow_run_id (unique); its project is the run's, reached through
    # that key, so it deliberately carries no project/owner column of its own
    # -- confirmed by direct look, not a drive-by bump.
    # +1 (`B13.2`): CaliberExecutionOwner is process-level lease state (one row
    # per lease name), not a project or user resource -- unscoped by design.
    # +1 (`B03.3`): CaliberPolicyDecision is immutable evidence keyed by run/grant ids
    # (no foreign keys: a decision about an unknown grant is still evidence) --
    # unscoped by design; its access is through the run and grant it names.
    # +1 (`B11.registry`): CaliberExperimentOwner is the system registry of who owns an MLflow experiment
    # (scope_kind/scope_id name the owner); it is read through experiment_registry and the admin routes only,
    # never listed to callers -- unscoped by design.
    # +1 (`B07.3a`): CaliberContextManifest is a write-once child of a workflow run (FK `workflow_run_id`) and carries
    # no project column of its own, like its sibling CaliberRunManifest: the run supplies the project boundary, and
    # the record names the project in its canonical bytes. Confirmed by direct look; no route lists it.
    # +1 (`B13.5a`): CaliberInboxEvent is the dedup reservation of an inbound event keyed by (consumer, source,
    # event_id) with no project column -- a webhook can arrive before any project is resolved. Unscoped, confirmed by
    # direct look; it is written and read only through `caliber.events.inbox` in the transaction it guards.
    # +1 (`B05.1`): CaliberEffectProposal is an immutable child of a workflow run (FK `workflow_run_id`) with no project
    # column of its own, like CaliberRunManifest: the run supplies the project boundary. Confirmed by direct look; it is
    # written and read only through `caliber.effects.records`, never listed through a route.
    # +1 (`B05.4a`): CaliberEffectReceipt is an immutable child of an effect proposal (FK `proposal_id`) with no project
    # column of its own: the proposal, and through it the run, supplies the boundary. Confirmed by direct look; it is
    # written and read only through `caliber.effects.receipts`, never listed through a route.
    # +1 (release evaluation recovery): CaliberWorkspaceReleaseEvaluationAttempt is an immutable child of a release evaluation (FK
    # `evaluation_id`) with no project column of its own: the evaluation, and through it the release, supplies the
    # project boundary, like the other subordinate audit rows above. Confirmed by direct look; it is written and read
    # only by the release evaluator and never listed through a route.
    # +1 (`B08.4`): CaliberStepTokenReservation is an immutable-once-final child of a workflow run (FK `workflow_run_id`) with no
    # project column of its own, like CaliberEffectProposal: the run supplies the project boundary. Confirmed by direct look; it is
    # written and read only through `caliber.budget.step_reservation`, never listed through a route.
    # +1 (`B09.2a`): CaliberAcceptanceDecision is an immutable child of a workflow run (FK `workflow_run_id`) with no project column
    # of its own, like CaliberEffectReceipt: the run supplies the project boundary. Confirmed by direct look; it is written and read
    # only through `caliber.acceptance.decision`, never listed through a route.
    SCOPING_UNSCOPED: 61,
    # -1 (`P1-E`): CaliberPersonalAccessToken gained project_id (optional
    # PAT project binding) and moves from owned_catalog to project_only --
    # see below.
    # +1 (`P2-O`): CaliberMcpServer gained project_id/visibility and now
    # participates in the shared three-tier resource boundary.
    # -1 (`P2-B`): CaliberAssistantSession gained a nullable project_id so a
    # multi-turn assistant conversation can retain its workspace context; it
    # is project_only because sessions intentionally have no visibility tier.
    # -1 (`P3-A` release FK): CaliberReworkTask gained a direct, nullable
    # project_id (populated only for a release-sourced task, since a
    # Workspace release has no single owning agent to derive one from) and
    # moves from owned_catalog to project_only. A job-sourced task's
    # project_id stays NULL -- its boundary is still derived live through
    # the source agent join in routes/rework_tasks.py, unchanged -- but the
    # column's mere presence is what this inventory classifies on.
    SCOPING_OWNED_CATALOG: 20,
    SCOPING_VISIBILITY: 15,
    # +1 (`P1-A`): CaliberWorkspaceEnvironment has project_id, no
    # visibility/owner column -- correctly project_only, confirmed by
    # direct look, not a drive-by bump.
    # +1 (`P1-E`): CaliberPersonalAccessToken now has project_id too, still
    # with no visibility column -- also project_only, confirmed by direct
    # look. Its `created_by` owner column still resolves fine (this tier
    # doesn't require the absence of one, only of `visibility`); the actual
    # binding is enforced by direct project_id equality in
    # `auth.py::resolve_identity`, not the 3-tier visibility scheme.
    # +3 (`P4-A`): source, import-job, and revision rows have direct
    # project_id columns but intentionally no visibility tier; their parent
    # Workspace is the authorization boundary.
    # +3 (`P4-D`): change requests, version claims, and version tags are
    # project-bound control-plane rows without a visibility tier.
    # +1 (`P4-E`): provider actor links are project-bound identity records
    # without a visibility tier.
    # +4 (`P5-A`): release, durable evaluation, break-glass authorization,
    # and release operation rows have direct project_id bindings and no
    # visibility tier. Their child evidence/decision/item rows are classified
    # as unscoped above because they inherit the same boundary through their
    # required parent FK.
    # +1 (`P5-D`): runtime lineage is a project-bound reconstruction record;
    # every consumer points to it, so the project remains queryable without
    # copying authorization columns onto each legacy run table.
    # +1 (`P3-A` release FK): CaliberReworkTask, see SCOPING_OWNED_CATALOG.
    # +1 (`P4-E`): CaliberWorkspaceSourceConnection (encrypted GitHub App
    # connection storage) has a direct project_id binding and no visibility
    # tier -- same shape as its sibling CaliberWorkspaceSourceActorLink
    # above, correctly project_only, confirmed by direct look. It carries
    # only secret-store references, never credential material itself.
    # +1 (`P2-R`): CaliberReleaseOperation, see SCOPING_UNSCOPED above --
    # same project_only shape as its newer sibling
    # CaliberWorkspaceReleaseOperation, confirmed by direct look.
    # +1 (`P2-Q`): CaliberVerificationItem, see SCOPING_UNSCOPED above --
    # same project_only shape as its sibling CaliberReleaseOperation,
    # confirmed by direct look. It carries only a bare project_id derived
    # from its own agent_id's target, no visibility/owner column.
    # +1 (`B03.2`): CaliberRunGrant has a required project_id (the project the
    # run acted in) and no visibility tier -- project_only, confirmed by direct
    # look; its access is the exact run/grant id plus current project membership.
    # +1 (`B13.4`): CaliberEventOutbox has a bare project_id (the project the
    # event belongs to) and no visibility tier -- project_only, confirmed by
    # direct look; rows are read only by the publisher, never through a route.
    # +1 (`B13.4`): CaliberEventInbox is a consumer's deduplication table keyed
    # by (consumer, event_id) with no project column -- unscoped, confirmed by
    # direct look; it is written and read only by the consumer that owns it.
    # +1 (`B07.1a`): CaliberApprovedContextSource has a required project_id and no visibility tier -- project_only,
    # confirmed by direct look; it is read and written only by `caliber.context.sources`, never listed through a route.
    SCOPING_PROJECT_ONLY: 29,
}


def test_every_known_model_is_classified_exactly_once() -> None:
    from caliber.db.base import Base

    rows = resource_inventory()
    names = [r.name for r in rows]
    assert len(names) == len(set(names)), "no model should appear twice"
    assert len(rows) == len(list(Base.registry.mappers))


def test_distribution_matches_todays_ratchet() -> None:
    rows = resource_inventory()
    counts = Counter(r.scoping for r in rows)
    assert dict(counts) == _EXPECTED_COUNTS, (
        f"scoping distribution changed: {dict(counts)} vs expected "
        f"{_EXPECTED_COUNTS}. If this is a deliberate new model, update "
        f"_EXPECTED_COUNTS here after confirming its scoping tier is "
        f"correct -- this is a ratchet meant to force that look, not a "
        f"number that should never move."
    )


def test_no_visibility_model_is_missing_project_id_or_an_owner_column() -> None:
    """The one genuinely load-bearing check: apply_visibility_filter's own
    docstring requires visibility + project_id + an owner column together.
    A model with visibility but missing either would raise the first time
    a non-admin hit it -- exactly the historical CaliberEvalRun defect
    db.scoping.owner_column's docstring names. Zero such cases exist today;
    this keeps it that way."""
    broken = [
        r.name
        for r in resource_inventory()
        if r.scoping == SCOPING_VISIBILITY and (not r.has_project_id or not r.owner_column)
    ]
    assert not broken, (
        f"these models have 'visibility' but are missing project_id or an "
        f"owner column, which would break apply_visibility_filter's "
        f"duck-typed contract: {broken}"
    )


def test_visibility_models_owner_column_call_succeeds_for_real() -> None:
    """Call-through, not re-derivation: proves the inventory's claim
    against the actual function routes depend on, not a second guess at
    what it would do."""
    from caliber.db import models

    for row in resource_inventory():
        if row.scoping != SCOPING_VISIBILITY:
            continue
        model = getattr(models, row.name)
        column = owner_column(model)  # must not raise
        assert column.key == row.owner_column


def test_project_only_models_have_no_visibility_column() -> None:
    for row in resource_inventory():
        if row.scoping == SCOPING_PROJECT_ONLY:
            assert row.has_project_id
            assert not row.has_visibility


def test_workspace_release_children_use_parent_project_boundary() -> None:
    rows = {row.name: row for row in resource_inventory()}

    assert {
        name: rows[name].scoping
        for name in (
            "CaliberWorkspaceRelease",
            "CaliberWorkspaceReleaseEvaluation",
            "CaliberWorkspaceBreakGlassAuthorization",
            "CaliberWorkspaceReleaseOperation",
        )
    } == dict.fromkeys(
        (
            "CaliberWorkspaceRelease",
            "CaliberWorkspaceReleaseEvaluation",
            "CaliberWorkspaceBreakGlassAuthorization",
            "CaliberWorkspaceReleaseOperation",
        ),
        SCOPING_PROJECT_ONLY,
    )
    for name in (
        "CaliberWorkspaceReleaseEvidence",
        "CaliberWorkspaceReleaseDecision",
        "CaliberWorkspaceReleaseOperationItem",
    ):
        assert rows[name].scoping == SCOPING_UNSCOPED
        assert not rows[name].has_project_id


def test_owned_catalog_models_have_no_project_id() -> None:
    for row in resource_inventory():
        if row.scoping == SCOPING_OWNED_CATALOG:
            assert row.owner_column is not None
            assert not row.has_project_id


def test_unscoped_models_have_neither_project_id_nor_owner_column() -> None:
    for row in resource_inventory():
        if row.scoping == SCOPING_UNSCOPED:
            assert not row.has_project_id
            assert row.owner_column is None
