"""The second real Workspace resource adapter -- a CALIBER prompt (`P4-B`/`P4-C`).

``workspace_release_workflow_adapter.py`` proved the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol end to end for release ``apply``/``observe``/``rollback``, but its
own ``resolve()``/``snapshot()`` are unused placeholders (see that module's
docstring: "Not called anywhere yet"). This module is the first adapter whose
``resolve()``/``snapshot()`` are load-bearing: ``POST
/projects/{id}/revisions:snapshot`` (``routes/workspace.py::snapshot_revision``)
calls them directly to pin a *live CALIBER resource* into an immutable
managed revision, independent of the Git-import path.

A prompt is the simplest well-understood resource type available for this
(`P2-G`'s already-closed hidden-prompt-target work): unlike every other
resource family, **no ``CaliberPrompt`` model exists at all** -- a prompt is
purely an MLflow Prompt Registry entity (flat, globally named, versioned by
MLflow itself), and the only CALIBER-side row is a hidden runtime identity,
:class:`~caliber.db.models.CaliberAgentConfig` keyed by
``agent_id == prompt_name`` (see ``prompt_targets.py``). That makes a prompt
adapter's ``resolve()`` genuinely two lookups rather than one: a CALIBER-side
project-scoping check against the hidden target (mirroring
``routes/prompts.py::create_prompt``'s `P2-G` collision refusal -- a prompt
already bound to a *different* project must not be snapshottable into this
one), and a live MLflow Prompt Registry read for the actual content.

Pin field convention this adapter establishes, mirroring
``workspace_release_workflow_adapter.py``'s own:

* ``resource_id`` -- the prompt's registry ``name``.
* ``version_ref`` -- the exact MLflow Prompt Registry version number, as a
  string. Snapshot requests are deliberately explicit-version-only (see
  ``routes/workspace.py::snapshot_revision``'s module docstring) -- this
  adapter never resolves an alias ("current"/"prod") into a version, because
  that would make the same snapshot request non-reproducible depending on
  when it runs.
* ``provider_ref`` -- the MLflow prompt URI actually read
  (``prompts:/{name}/{version}``).

``apply_release``/``rollback_release``/``observe_release`` promote a prompt
alias via MLflow's own ``set_prompt_alias``/``load_prompt`` APIs -- a
genuinely external provider (unlike the workflow adapter's same-database
alias rotation), so they keep the original two-phase apply/observe contract
(see ``workspace_release_adapters.py``'s module docstring) rather than
sharing the caller's session for a provider mutation.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberProject,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)

logger = logging.getLogger(__name__)

_TARGET_REF_PREFIX = "prompt:"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention
#: ``workspace_import_materializer.py::MATERIALIZER_ADAPTER_VERSION`` uses.
PROMPT_ADAPTER_VERSION = "workspace-release-prompt-adapter/1"


@dataclass(frozen=True)
class ResolvedPromptVersion:
    """An MLflow Prompt Registry version, loaded and project-checked."""

    name: str
    version: int
    template: str
    tags: Mapping[str, object]
    provider_ref: str


def _target_ref(name: str, alias: str) -> str:
    return f"{_TARGET_REF_PREFIX}{name}@{alias}"


def _parse_target_ref(target_ref: str) -> tuple[str, str]:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or "@" not in target_ref:
        raise WorkspaceReleaseAdapterError(f"malformed prompt target_ref {target_ref!r}")
    name, _, alias = target_ref[len(_TARGET_REF_PREFIX) :].partition("@")
    if not name or not alias:
        raise WorkspaceReleaseAdapterError(f"malformed prompt target_ref {target_ref!r}")
    return name, alias


class PromptWorkspaceResourceAdapter:
    """Resolves, snapshots, validates, and releases a CALIBER prompt."""

    resource_type = "prompt"

    # -- MLflow access -----------------------------------------------------
    # Self-contained lazy-import helpers (not imported from routes/prompts.py)
    # so this adapter has no dependency on the route layer -- mirrors
    # routes/prompts.py's own _get_mlflow_module/_resolve_api shape, which is
    # the established pattern for "mlflow is an optional runtime dependency"
    # in this codebase.

    def _mlflow_module(self) -> ModuleType | None:
        try:
            import mlflow  # noqa: PLC0415

            return mlflow
        except ImportError:
            logger.warning("mlflow not installed; prompt adapter operations unavailable")
            return None

    def _api(self, mlflow_mod: ModuleType, name: str) -> Callable[..., Any] | None:
        genai = getattr(mlflow_mod, "genai", None)
        if genai is not None:
            fn = getattr(genai, name, None)
            if callable(fn):
                return cast(Callable[..., Any], fn)
        fn = getattr(mlflow_mod, name, None)
        if callable(fn):
            return cast(Callable[..., Any], fn)
        return None

    # -- resolve / snapshot --------------------------------------------------

    def resolve(
        self, session: object, workspace: object, declaration: object
    ) -> ResolvedPromptVersion:
        """Load one exact prompt version and enforce project scoping.

        ``declaration`` carries ``resource_id`` (the prompt name) and
        ``version_ref`` (the exact MLflow version, as a string/int) --
        the shape ``routes/workspace.py::snapshot_revision`` builds from
        its request body's explicit pin list.
        """
        assert isinstance(session, Session)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("prompt declaration must be a mapping")
        name = declaration.get("resource_id") or declaration.get("prompt_name")
        raw_version = declaration.get("version_ref", declaration.get("version"))
        if not name or raw_version is None:
            raise WorkspaceReleaseAdapterError(
                "prompt declaration requires 'resource_id' and 'version_ref'"
            )
        name = str(name)
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise WorkspaceReleaseAdapterError(
                f"prompt version {raw_version!r} must be an integer"
            ) from exc

        project = workspace if isinstance(workspace, CaliberProject) else None
        target = session.get(CaliberAgentConfig, name)
        if (
            target is not None
            and target.project_id is not None
            and project is not None
            and target.project_id != project.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"prompt {name!r} is registered to project {target.project_id!r}, "
                f"not {project.project_id!r}"
            )

        mlflow_mod = self._mlflow_module()
        if mlflow_mod is None:
            raise WorkspaceReleaseAdapterError("mlflow is not installed")
        load_prompt = self._api(mlflow_mod, "load_prompt")
        if load_prompt is None:
            raise WorkspaceReleaseAdapterError("mlflow prompt registry API not available")

        ref = f"prompts:/{name}/{version}"
        try:
            prompt = load_prompt(ref, allow_missing=True)
        except Exception as exc:
            raise WorkspaceReleaseAdapterError(f"failed to load prompt {ref!r}: {exc}") from exc
        if prompt is None:
            raise WorkspaceReleaseAdapterError(
                f"prompt {ref!r} not found in the MLflow Prompt Registry"
            )

        template = getattr(prompt, "template", None) or getattr(prompt, "content", None) or ""
        raw_tags = getattr(prompt, "tags", None)
        tags = dict(raw_tags) if isinstance(raw_tags, Mapping) else {}
        return ResolvedPromptVersion(
            name=str(getattr(prompt, "name", name)),
            version=version,
            template=str(template),
            tags=tags,
            provider_ref=ref,
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the loaded template.

        Unlike ``WorkflowWorkspaceResourceAdapter.snapshot()`` (still an
        explicit placeholder that returns its input unchanged -- see that
        module's docstring), this is real: the returned digest is what a
        caller diffing two revisions or verifying a release actually
        authenticates against.
        """
        if not isinstance(resolved_pin, ResolvedPromptVersion):
            raise WorkspaceReleaseAdapterError("prompt snapshot requires a resolved prompt version")
        content_sha256 = hashlib.sha256(resolved_pin.template.encode("utf-8")).hexdigest()
        return SnapshotPin(
            resource_id=resolved_pin.name,
            version_ref=str(resolved_pin.version),
            content_sha256=content_sha256,
            provider_ref=resolved_pin.provider_ref,
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": PROMPT_ADAPTER_VERSION,
                "template_length": len(resolved_pin.template),
            },
        )

    # -- validate ------------------------------------------------------------

    def validate(
        self,
        session: object,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment | None = None,
    ) -> dict[str, Any]:
        assert isinstance(session, Session)
        name = pin.resource_id
        target = session.get(CaliberAgentConfig, name)
        if (
            target is not None
            and target.project_id is not None
            and environment is not None
            and target.project_id != environment.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"prompt {name!r} belongs to project {target.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        raw_version = pin.provider_ref or pin.version_ref
        version = raw_version.rsplit("/", 1)[-1] if raw_version else pin.version_ref
        try:
            version_number = int(version)
        except (TypeError, ValueError) as exc:
            raise WorkspaceReleaseAdapterError(
                f"prompt {name!r} pin has a non-integer version {pin.version_ref!r}"
            ) from exc
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "prompt_name": name,
            "version": version_number,
        }

    # -- prepare ---------------------------------------------------------

    def prepare_release(
        self,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment,
        *,
        before_ref: str | None,
    ) -> PreparedAction:
        after_ref = pin.version_ref
        return PreparedAction(
            resource_pin_id=pin.resource_pin_id,
            resource_type=self.resource_type,
            action="no_op" if before_ref == after_ref else "promote",
            target_ref=_target_ref(pin.resource_id, environment.name),
            before_ref=before_ref,
            after_ref=after_ref,
            metadata={
                "prompt_name": pin.resource_id,
                "alias": environment.name,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
            },
        )

    # -- apply / observe / rollback ---------------------------------------
    # A real network-shaped provider (the MLflow Prompt Registry), unlike the
    # workflow adapter's same-database alias rotation -- see this module's
    # docstring and workspace_release_adapters.py's for why that keeps the
    # original two-phase apply/observe contract rather than sharing the
    # caller's session.

    def apply_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
        return self._promote(prepared)

    def rollback_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
        return self._promote(prepared)

    def _promote(self, prepared: PreparedAction) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"prompt-no-op:{prepared.resource_pin_id}",
                provider_result={"version": prepared.after_ref},
            )
        name, alias = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to promote"
            )
        try:
            version_number = int(after_ref)
        except (TypeError, ValueError):
            return ProviderOutcome(
                "failed",
                f"prompt-invalid-version:{name}",
                error_code="invalid_prompt_version",
                error_summary=f"after_ref {after_ref!r} is not an integer prompt version",
            )
        mlflow_mod = self._mlflow_module()
        if mlflow_mod is None:
            return ProviderOutcome(
                "failed",
                f"prompt-mlflow-unavailable:{name}",
                error_code="mlflow_unavailable",
                error_summary="mlflow is not installed",
            )
        set_prompt_alias = self._api(mlflow_mod, "set_prompt_alias")
        if set_prompt_alias is None:
            return ProviderOutcome(
                "failed",
                f"prompt-mlflow-api-unavailable:{name}",
                error_code="mlflow_api_unavailable",
                error_summary="mlflow prompt registry alias API not available",
            )
        try:
            set_prompt_alias(name, alias, version_number)
        except Exception as exc:
            return ProviderOutcome(
                "failed",
                f"prompt-alias-failed:{name}:{alias}",
                error_code="prompt_alias_set_failed",
                error_summary=str(exc),
            )
        return ProviderOutcome(
            "applied",
            f"prompt:{name}@{alias}:{version_number}",
            provider_result={"version": version_number},
        )

    def observe_release(self, _session: object, prepared: PreparedAction) -> ProviderOutcome:
        name, alias = _parse_target_ref(prepared.target_ref)
        mlflow_mod = self._mlflow_module()
        if mlflow_mod is None:
            return ProviderOutcome(
                "failed",
                f"prompt-mlflow-unavailable:{name}",
                error_code="mlflow_unavailable",
                error_summary="mlflow is not installed",
            )
        load_prompt = self._api(mlflow_mod, "load_prompt")
        if load_prompt is None:
            return ProviderOutcome(
                "failed",
                f"prompt-mlflow-api-unavailable:{name}",
                error_code="mlflow_api_unavailable",
                error_summary="mlflow prompt registry load API not available",
            )
        ref = f"prompts:/{name}@{alias}"
        try:
            prompt = load_prompt(ref, allow_missing=True)
        except Exception as exc:
            return ProviderOutcome(
                "failed",
                f"prompt-observe-error:{name}:{alias}",
                error_code="prompt_observe_failed",
                error_summary=str(exc),
            )
        if prompt is None:
            return ProviderOutcome(
                "failed",
                f"prompt-observe-missing:{name}:{alias}",
                error_code="prompt_alias_not_found",
                error_summary=f"no alias {alias!r} for prompt {name!r}",
            )
        observed_version = getattr(prompt, "version", None)
        if observed_version is None or str(observed_version) != str(prepared.after_ref):
            return ProviderOutcome(
                "failed",
                f"prompt-observe-mismatch:{name}:{alias}",
                error_code="target_not_observed",
                error_summary=(
                    f"alias {alias!r} points at {observed_version!r}, "
                    f"expected {prepared.after_ref!r}"
                ),
            )
        return ProviderOutcome(
            "applied",
            f"prompt:{name}@{alias}:{observed_version}",
            provider_result={"version": observed_version},
        )


__all__ = ["PROMPT_ADAPTER_VERSION", "PromptWorkspaceResourceAdapter", "ResolvedPromptVersion"]
