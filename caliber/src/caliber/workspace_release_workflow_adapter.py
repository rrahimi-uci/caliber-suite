"""The first real (non-fake) Workspace release adapter (`P5-E`).

Phase 5 built a complete, tested release state machine
(:mod:`caliber.workspace_release_operations`, :mod:`caliber.workspace_release_governance`)
but the only :class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
ever instantiated outside a test file was
:class:`~caliber.workspace_release_adapters.FakeWorkspaceResourceAdapter` -- a
Workspace release could be fully modeled end to end and could not actually
deploy anything. This module closes that gap for one resource type: a
Workflow Studio workflow, whose deploy mechanics
(:func:`caliber.workflows.promoter.rotate_alias_to_version`) are already
mature, deterministic, and offline-testable.

This adapter's "provider" is the same database the release operation itself
runs against -- rotating a deployment alias is just another row in
``caliber_workflow_deployments`` -- so ``apply_release``/``observe_release``/
``rollback_release`` use the *caller's own session*
(see :mod:`caliber.workspace_release_adapters`'s module docstring for why:
a second connection writing while the caller's transaction is open
deadlocks on SQLite's single-writer lock, and sharing the transaction is
also more correct for a same-database provider regardless of engine).

Pin field convention this adapter establishes (`P4-B`/`P4-C`'s managed-
snapshot epic's seventh and final slice made ``resolve()``/``snapshot()``
real against this exact convention, rather than inventing a new one --
see this module's ``resolve()``/``snapshot()`` docstrings below):

* ``resource_id`` -- the workflow's ``workflow_id``.
* ``version_ref`` -- the ``version_id`` the revision pinned.
* ``provider_ref`` -- the ``version_id`` actually deployed on release
  (defaults to ``version_ref`` when unset, so a pin that never diverged
  from its declared version needs no separate provider ref).

Deliberately out of scope, named rather than silently skipped:

* :func:`caliber.workflows.promoter.evaluate_deploy_gates` (the eval-dataset
  quality gate) is never run here -- it replays real examples through the
  configured model, which is expensive and non-deterministic in a way this
  adapter's apply/observe/rollback triad is not. Rather than silently
  deploying past a gate the workflow's own author configured,
  :meth:`WorkflowWorkspaceResourceAdapter._refuse_if_gated` fails the
  release closed when the target version has a deploy gate configured for
  the target alias.
* Revision materialization (writing real pins from an import) is a
  separate, not-yet-built slice; this adapter is exercised by directly
  seeded pins in tests, exactly as
  :class:`~caliber.workspace_release_adapters.FakeWorkspaceResourceAdapter`
  already is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.db.scoping import get_visible
from caliber.workflows.manifest import compute_manifest_hash, parse_manifest
from caliber.workflows.promoter import AliasPreflightError, rotate_alias_to_version
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)

logger = logging.getLogger(__name__)

_TARGET_REF_PREFIX = "workflow:"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention every sibling
#: adapter's own ``*_ADAPTER_VERSION`` constant establishes (e.g.
#: ``workspace_release_skill_adapter.py::SKILL_ADAPTER_VERSION``).
WORKFLOW_ADAPTER_VERSION = "workspace-release-workflow-adapter/1"


def _target_ref(workflow_id: str, alias: str) -> str:
    return f"{_TARGET_REF_PREFIX}{workflow_id}@{alias}"


def _parse_target_ref(target_ref: str) -> tuple[str, str]:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or "@" not in target_ref:
        raise WorkspaceReleaseAdapterError(f"malformed workflow target_ref {target_ref!r}")
    workflow_id, _, alias = target_ref[len(_TARGET_REF_PREFIX) :].partition("@")
    if not workflow_id or not alias:
        raise WorkspaceReleaseAdapterError(f"malformed workflow target_ref {target_ref!r}")
    return workflow_id, alias


@dataclass(frozen=True)
class ResolvedWorkflowVersion:
    """One immutable, published ``caliber_workflow_versions`` row, loaded and
    visibility-checked against its live parent ``CaliberWorkflow``."""

    workflow_id: str
    name: str
    version_id: str
    version_number: int
    manifest: dict[str, Any]
    provider_ref: str


class WorkflowWorkspaceResourceAdapter:
    """Deploys a Workspace release's workflow pins by rotating a deployment alias."""

    resource_type = "workflow"

    def __init__(
        self,
        *,
        config: CaliberConfig | None = None,
        actor: str = "workspace-release-worker",
    ) -> None:
        self._config = config
        self._actor = actor

    # -- resolve / snapshot ---------------------------------------------
    # `P4-B`/`P4-C`'s managed-snapshot epic's seventh and final slice: the
    # last of the epic's follow-up list to move off the placeholder that just
    # returned `resolve()`'s own input unchanged (see docs/workspace-plan.md's
    # `P4-B` row). This mirrors `SkillWorkspaceResourceAdapter`'s exact shape,
    # the closest structural precedent: a workflow is *also* both a mutable
    # parent row (`CaliberWorkflow` -- identity, ownership, 3-tier
    # `project`/`user`/`public` visibility) and a real, internal, immutable,
    # numbered content history (`CaliberWorkflowVersion` -- unlike a skill
    # version, published rows are already documented elsewhere as immutable,
    # see `routes/workflow_versions.py`'s own module docstring: "Published
    # versions are immutable"). `resolve()` is accordingly two lookups
    # against the same database: a `get_visible` 3-tier visibility check
    # against the live `CaliberWorkflow` row (the same non-negotiable fix
    # `prompt`'s own PR #393 needed as a follow-up, applied here from the
    # start -- see `workspace_release_adapters.py`'s module docstring), then
    # an exact `(workflow_id, version_id)` read against
    # `caliber_workflow_versions` for the pinned version's manifest.
    #
    # Unlike every sibling adapter's `version_ref` (an integer version
    # number, or a content-digest/`"current"` sentinel for a resource with no
    # numbered history), this adapter's own pre-existing, already-relied-upon
    # convention (`apply_release`/`validate`/`prepare_release` below, and
    # `P5-E`'s alias-rotation machinery) is that `version_ref`/`provider_ref`
    # carry the workflow version's own `version_id` primary key, not its
    # `version_number` -- `rotate_alias_to_version` takes a `version_id`
    # directly, and `validate()`/`prepare_release()` already read
    # `pin.version_ref` as one. `resolve()`/`snapshot()` reuse that exact
    # convention rather than inventing a numbered scheme of their own, so a
    # pin this method produces plugs into the release path with no
    # translation step.

    def resolve(
        self, session: object, _workspace: object, declaration: object, identity: object
    ) -> ResolvedWorkflowVersion:
        """Load one exact, published workflow version through the live
        workflow's own 3-tier visibility model.

        ``declaration`` carries ``resource_id`` (the workflow's
        ``workflow_id``) and ``version_ref`` (the exact
        ``caliber_workflow_versions.version_id`` this pin targets -- never an
        alias, mirroring every sibling adapter's explicit-version-only
        reproducibility stance: resolving an alias like ``"prod"`` would make
        the same snapshot request produce different content depending on
        when it runs). Only a **published** version is snapshot-eligible: a
        draft's ``manifest`` can still be edited in place
        (``routes/workflow_versions.py::update_version``), so pinning one
        would not actually be content-addressed -- a later edit would
        silently invalidate the digest this method's own ``snapshot()``
        computed without changing the pin itself.
        """
        assert isinstance(session, Session)
        assert isinstance(identity, CaliberIdentity)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("workflow declaration must be a mapping")
        workflow_id = declaration.get("resource_id") or declaration.get("workflow_id")
        version_id = declaration.get("version_ref") or declaration.get("version_id")
        if not workflow_id or not version_id:
            raise WorkspaceReleaseAdapterError(
                "workflow declaration requires 'resource_id' and 'version_ref'"
            )
        workflow_id = str(workflow_id)
        version_id = str(version_id)

        workflow = get_visible(
            session, CaliberWorkflow, CaliberWorkflow.workflow_id, workflow_id, identity
        )
        if workflow is None:
            # Matches `routes/workflows.py::get_workflow`'s own "not found"
            # 404 for both a genuinely missing workflow and one that exists
            # but is not visible to this caller -- never distinguishing the
            # two, so this can't be used to probe for another user's
            # workflow ids.
            raise WorkspaceReleaseAdapterError(
                f"workflow {workflow_id!r} not found or not visible to the caller"
            )

        version = (
            session.execute(
                select(CaliberWorkflowVersion).where(
                    CaliberWorkflowVersion.workflow_id == workflow_id,
                    CaliberWorkflowVersion.version_id == version_id,
                )
            )
            .scalars()
            .first()
        )
        if version is None:
            raise WorkspaceReleaseAdapterError(
                f"workflow {workflow_id!r} has no recorded version {version_id!r}"
            )
        if version.status != "published":
            raise WorkspaceReleaseAdapterError(
                f"workflow {workflow_id!r} version {version_id!r} is "
                f"{version.status!r}; only a published version can be snapshotted "
                "(a draft's manifest can still be edited in place)"
            )

        return ResolvedWorkflowVersion(
            workflow_id=workflow.workflow_id,
            name=workflow.name,
            version_id=version.version_id,
            version_number=version.version_number,
            manifest=version.manifest,
            provider_ref=f"caliber-workflow-version:/{workflow_id}/{version_id}",
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the version's manifest --
        the compiled node graph that determines what the workflow actually
        does when deployed/run -- reusing
        :func:`caliber.workflows.manifest.compute_manifest_hash`, the same
        canonical hashing routine every workflow route already uses for a
        version's own ``manifest_hash`` column
        (``routes/workflows.py::import_workflow``,
        ``routes/workflow_versions.py::_insert_version_with_retry``/
        ``update_version``), rather than inventing a second, adapter-local
        hashing convention for the same content.
        """
        if not isinstance(resolved_pin, ResolvedWorkflowVersion):
            raise WorkspaceReleaseAdapterError(
                "workflow snapshot requires a resolved workflow version"
            )
        content_sha256 = compute_manifest_hash(resolved_pin.manifest)
        return SnapshotPin(
            resource_id=resolved_pin.workflow_id,
            version_ref=resolved_pin.version_id,
            content_sha256=content_sha256,
            provider_ref=resolved_pin.provider_ref,
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": WORKFLOW_ADAPTER_VERSION,
                "version_number": resolved_pin.version_number,
            },
        )

    # -- validate ----------------------------------------------------------
    # Not called by workspace_release_operations.py today (nothing in that
    # module calls adapter.validate() at all), but kept real and exercised
    # directly since it is the cheapest place to prove the same checks
    # apply/rollback enforce.

    def validate(
        self,
        session: object,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment | None = None,
    ) -> dict[str, Any]:
        assert isinstance(session, Session)
        workflow = session.get(CaliberWorkflow, pin.resource_id)
        if workflow is None:
            raise WorkspaceReleaseAdapterError(f"workflow {pin.resource_id!r} not found")
        if environment is not None and workflow.project_id != environment.project_id:
            raise WorkspaceReleaseAdapterError(
                f"workflow {pin.resource_id!r} belongs to project {workflow.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        version_id = pin.provider_ref or pin.version_ref
        gate_alias = environment.name if environment is not None else None
        if gate_alias is not None:
            self._refuse_if_gated(session, version_id, gate_alias)
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "workflow_id": pin.resource_id,
            "version_id": version_id,
        }

    # -- prepare -------------------------------------------------------
    # No session, no provider I/O -- pure ref computation, exactly like
    # FakeWorkspaceResourceAdapter.prepare_release.

    def prepare_release(
        self,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment,
        *,
        before_ref: str | None,
    ) -> PreparedAction:
        after_ref = pin.provider_ref or pin.version_ref
        return PreparedAction(
            resource_pin_id=pin.resource_pin_id,
            resource_type=self.resource_type,
            action="no_op" if before_ref == after_ref else "promote",
            target_ref=_target_ref(pin.resource_id, environment.name),
            before_ref=before_ref,
            after_ref=after_ref,
            metadata={
                "workflow_id": pin.resource_id,
                "alias": environment.name,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
            },
        )

    # -- apply / rollback -----------------------------------------------
    # Both share the caller's session (see module docstring) and only
    # flush -- the caller's own eventual commit persists the operation's
    # state and this deployment write together, atomically.

    def apply_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._rotate(session, prepared)

    def rollback_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._rotate(session, prepared)

    def _rotate(self, session: Session, prepared: PreparedAction) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"workflow-no-op:{prepared.resource_pin_id}",
                provider_result={"version_id": prepared.after_ref},
            )
        workflow_id, alias = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to deploy"
            )
        project_id = prepared.metadata.get("project_id")
        workflow = session.get(CaliberWorkflow, workflow_id)
        if workflow is None:
            return ProviderOutcome(
                "failed",
                f"workflow-missing:{workflow_id}",
                error_code="workflow_not_found",
                error_summary=f"workflow {workflow_id!r} not found",
            )
        if project_id is not None and workflow.project_id != project_id:
            return ProviderOutcome(
                "failed",
                f"workflow-tenancy:{workflow_id}",
                error_code="resource_outside_project",
                error_summary=(
                    f"workflow {workflow_id!r} belongs to project {workflow.project_id!r}, "
                    f"not release project {project_id!r}"
                ),
            )
        gated = self._refuse_if_gated(session, after_ref, alias, raise_on_gate=False)
        if gated is not None:
            return gated
        try:
            deployment = rotate_alias_to_version(
                session,
                workflow_id,
                alias,
                after_ref,
                actor=self._actor,
                config=self._config,
            )
        except AliasPreflightError as exc:
            return ProviderOutcome(
                "failed",
                f"workflow-preflight:{workflow_id}:{alias}",
                error_code="alias_preflight_failed",
                error_summary=str(exc),
            )
        session.flush()
        return ProviderOutcome(
            "applied",
            deployment.deployment_id,
            provider_result={"version_id": deployment.version_id},
        )

    # -- observe ---------------------------------------------------------

    def observe_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        workflow_id, alias = _parse_target_ref(prepared.target_ref)
        deployment = (
            session.execute(
                select(CaliberWorkflowDeployment).where(
                    CaliberWorkflowDeployment.workflow_id == workflow_id,
                    CaliberWorkflowDeployment.alias == alias,
                )
            )
            .scalars()
            .first()
        )
        if deployment is None:
            return ProviderOutcome(
                "failed",
                f"workflow-observe-missing:{workflow_id}:{alias}",
                error_code="deployment_not_found",
                error_summary=f"no deployment row for {workflow_id!r}/{alias!r}",
            )
        if deployment.version_id == prepared.after_ref:
            return ProviderOutcome(
                "applied",
                deployment.deployment_id,
                provider_result={"version_id": deployment.version_id},
            )
        return ProviderOutcome(
            "failed",
            deployment.deployment_id,
            error_code="target_not_observed",
            error_summary=(
                f"deployment {alias!r} points at {deployment.version_id!r}, "
                f"expected {prepared.after_ref!r}"
            ),
        )

    # -- shared safety check ----------------------------------------------

    def _refuse_if_gated(
        self,
        session: Session,
        version_id: str,
        alias: str,
        *,
        raise_on_gate: bool = True,
    ) -> ProviderOutcome | None:
        """Fail closed rather than silently skip a configured deploy gate.

        ``rotate_alias_to_version`` deliberately does not run
        :func:`~caliber.workflows.promoter.evaluate_deploy_gates` (see this
        module's docstring). Without this check a Workspace release to
        ``prod`` would rotate straight past a gate the workflow's own author
        configured for that alias.
        """
        version = session.get(CaliberWorkflowVersion, version_id)
        if version is None:
            return None  # rotate_alias_to_version's own preflight reports this
        try:
            manifest = parse_manifest(version.manifest or {})
        except Exception:  # pragma: no cover - malformed manifests fail preflight instead
            return None
        gated = any(alias in gate.required_for_aliases for gate in manifest.deploy_gates.values())
        if not gated:
            return None
        message = (
            f"workflow {version.workflow_id!r} has a deploy gate configured for alias "
            f"{alias!r}; Workspace release execution does not evaluate deploy gates yet "
            "-- promote through the workflow's own promotion endpoint instead"
        )
        if raise_on_gate:
            raise WorkspaceReleaseAdapterError(message)
        return ProviderOutcome(
            "failed",
            f"workflow-gated:{version.workflow_id}:{alias}",
            error_code="deploy_gate_not_evaluated",
            error_summary=message,
        )


__all__ = [
    "WORKFLOW_ADAPTER_VERSION",
    "ResolvedWorkflowVersion",
    "WorkflowWorkspaceResourceAdapter",
]
