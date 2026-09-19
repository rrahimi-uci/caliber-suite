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

Pin field convention this adapter establishes (nothing writes
:class:`~caliber.db.models.CaliberWorkspaceRevisionResource` rows in
production yet -- see docs/workspace-plan.md's `P5-E` row):

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
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.promoter import AliasPreflightError, rotate_alias_to_version
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    WorkspaceReleaseAdapterError,
)

logger = logging.getLogger(__name__)

_TARGET_REF_PREFIX = "workflow:"


def _target_ref(workflow_id: str, alias: str) -> str:
    return f"{_TARGET_REF_PREFIX}{workflow_id}@{alias}"


def _parse_target_ref(target_ref: str) -> tuple[str, str]:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or "@" not in target_ref:
        raise WorkspaceReleaseAdapterError(f"malformed workflow target_ref {target_ref!r}")
    workflow_id, _, alias = target_ref[len(_TARGET_REF_PREFIX) :].partition("@")
    if not workflow_id or not alias:
        raise WorkspaceReleaseAdapterError(f"malformed workflow target_ref {target_ref!r}")
    return workflow_id, alias


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
    # `routes/workspace.py::snapshot_revision` (`P4-C`) is this Protocol's
    # first real caller for any resource_type, but a "workflow" pin always
    # fails here today: that route's declaration shape is generic
    # (`resource_id`/`version_ref`), and this method still expects its own
    # pre-existing `workflow_id` key, so it never reaches -- let alone
    # returns -- real content. `_identity` is accordingly unused: this
    # adapter's `resolve()` has no real caller to authorize against yet, the
    # same reason `snapshot()` right below is still a placeholder.

    def resolve(
        self, session: object, _workspace: object, declaration: object, _identity: object
    ) -> CaliberWorkflow:
        # The Protocol types `session` as `object` so this module need not be
        # imported wherever the Protocol is (see workspace_release_adapters's
        # module docstring); every real caller passes a live Session.
        assert isinstance(session, Session)
        workflow_id = declaration.get("workflow_id") if isinstance(declaration, Mapping) else None
        if not workflow_id:
            raise WorkspaceReleaseAdapterError("workflow declaration requires 'workflow_id'")
        workflow = session.get(CaliberWorkflow, str(workflow_id))
        if workflow is None:
            raise WorkspaceReleaseAdapterError(f"workflow {workflow_id!r} not found")
        return workflow

    def snapshot(self, _session: object, resolved_pin: object) -> object:
        return resolved_pin

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


__all__ = ["WorkflowWorkspaceResourceAdapter"]
