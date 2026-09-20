"""The third real Workspace resource adapter -- a CALIBER registered tool
(`P4-B`/`P4-C`, slice 2 of the multi-slice managed-snapshot epic).

``workspace_release_prompt_adapter.py`` was this Protocol's first genuinely
content-addressed ``resolve()``/``snapshot()`` pair, resolving a prompt
through *two* lookups: a CALIBER-side visibility check against a hidden
``CaliberAgentConfig`` target, then a separate, external MLflow Prompt
Registry read for the actual content -- because no ``CaliberPrompt`` model
exists at all. A tool is the opposite shape: :class:`~caliber.db.models.CaliberToolRegistry`
(``routes/tools.py``) is *simultaneously* the identity, the visibility record,
and the content store. A single ``(name, version)`` row -- unique globally,
not just per project (``uq_tool_name_version``, enforced with no project
filter in ``routes/tools.py::register_tool``) -- carries the tool's own
``project_id``/``visibility``/``owner`` columns *and* its executable
definition (``module_path``/``callable_name``/``execution_backend``/
``backend_config``/``input_schema``/``output_schema``) *and* its safety
policy (``side_effect_level``/``requires_approval``/``allow_in_preview``/
``secret_refs``). So ``resolve()`` here is genuinely **one** lookup: a single
``SELECT ... WHERE name = :name AND version = :version`` run *through*
:func:`caliber.db.scoping.apply_visibility_filter` (the exact same 3-tier
``project``/``user``/``public`` predicate ``routes/tools.py::_visible_tool_or_404``
already applies to every tool route), rather than a bare ``project_id``
equality check that -- as `P4-C`'s prompt-adapter security fix already
proved for a different resource type -- cannot distinguish "public" from
"another user's unshared personal resource" when both read ``project_id IS
NULL``. Filtering the single-row query through the visibility predicate
closes that gap from the start rather than reintroducing it.

Pin field convention this adapter establishes, mirroring
``workspace_release_prompt_adapter.py``'s own:

* ``resource_id`` -- the tool's ``name`` (its family, shared across versions).
* ``version_ref`` -- the tool's own ``version`` string. Unlike a prompt
  (versioned by MLflow, an integer) or a workflow (versioned by an opaque
  ``version_id``, via ``CaliberWorkflowVersion``), a tool's ``version`` is
  already a free-form string column on the registry row itself
  (``routes/tools.py::_tool_version_sort_key`` sorts it component-wise
  because it is not guaranteed to be a bare integer) -- there is no separate
  version identity to resolve or convert, so this adapter keeps it a string
  throughout rather than forcing an ``int()`` cast the way the prompt adapter
  does for its own genuinely-integer MLflow version.
* ``provider_ref`` -- a synthetic ``caliber-tool-registry:/{tool_id}``
  reference. There is no second, external system a tool is read from (unlike
  a prompt's ``prompts:/{name}/{version}`` MLflow URI) -- the "provider" is
  this same CALIBER database -- but recording the exact row id read is still
  useful for audit/debugging, the same way a real external ref would be.

Content hash: unlike an MLflow prompt version (immutable the instant it is
registered) a ``CaliberToolRegistry`` row is a **single mutable row per
``(name, version)``** -- ``routes/tools.py::update_tool`` can change
``description``/``side_effect_level``/``requires_approval``/
``allow_in_preview``/``owner``/``status``/``successor_tool_id`` on the exact
same row *after* it was registered (and after a Workspace snapshot may have
already pinned it). So ``content_sha256`` here is a genuine point-in-time
digest over the fields that determine what the tool actually *does* and
under what safety policy -- ``module_path``, ``callable_name``,
``execution_backend``, ``backend_config``, ``input_schema``,
``output_schema``, ``side_effect_level``, ``requires_approval``,
``allow_in_preview``, ``secret_refs`` -- deliberately excluding purely
descriptive/administrative fields (``description``, ``owner``,
``successor_tool_id``, ``status``, ``test_cases``, ``last_calibration``)
that do not change what invoking the tool does. Because the row can drift
after being pinned, ``apply_release``/``rollback_release``/``observe_release``
below re-read the live row and refuse to proceed (``tool_content_drifted``)
if its current content hash no longer matches the pin, rather than silently
deploying content the release never actually reviewed.

``apply_release``/``rollback_release``/``observe_release``: a tool has no
runtime alias/deployment pointer of its own to flip -- a Workflow Studio
workflow references an exact tool ``(name, major-version)`` pair directly in
its own manifest (``routes/tools.py::_invoke_tool_under_preview_policy``'s
``registry_ref``), so "deploying" a tool to a Workspace environment does not
rotate anything the way a prompt alias or workflow deployment alias does.
This adapter's "provider" is therefore the same database the release
operation itself runs against (like
:mod:`caliber.workspace_release_workflow_adapter`, unlike the prompt
adapter's genuinely external MLflow calls) -- it shares the caller's own
session (see ``workspace_release_adapters.py``'s module docstring for why:
a second connection attempting a write while the caller's transaction is
open would deadlock on SQLite's single-writer lock) and only ever *reads*
through it to verify the pinned tool version still exists, is not archived,
and has not drifted. There is nothing to mutate, so a successful outcome is
always a verification, never a state change -- ``action`` is ``"verify"``
rather than ``"promote"`` for exactly this reason, to avoid implying an
effect that does not exist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberToolRegistry,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.workflows.manifest import canonical_json
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)

_TARGET_REF_PREFIX = "tool:"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention
#: ``workspace_release_prompt_adapter.py::PROMPT_ADAPTER_VERSION`` uses.
TOOL_ADAPTER_VERSION = "workspace-release-tool-adapter/1"


@dataclass(frozen=True)
class ResolvedToolVersion:
    """One ``CaliberToolRegistry`` row, loaded and visibility-checked."""

    tool_id: str
    name: str
    version: str
    module_path: str
    callable_name: str
    execution_backend: str
    backend_config: Mapping[str, Any] | None
    input_schema: Mapping[str, Any] | None
    output_schema: Mapping[str, Any] | None
    side_effect_level: str
    requires_approval: bool
    allow_in_preview: bool
    secret_refs: tuple[str, ...]
    status: str
    provider_ref: str


def _target_ref(name: str, alias: str) -> str:
    return f"{_TARGET_REF_PREFIX}{name}@{alias}"


def _parse_target_ref(target_ref: str) -> tuple[str, str]:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or "@" not in target_ref:
        raise WorkspaceReleaseAdapterError(f"malformed tool target_ref {target_ref!r}")
    name, _, alias = target_ref[len(_TARGET_REF_PREFIX) :].partition("@")
    if not name or not alias:
        raise WorkspaceReleaseAdapterError(f"malformed tool target_ref {target_ref!r}")
    return name, alias


def _content_payload(
    *,
    module_path: str,
    callable_name: str,
    execution_backend: str,
    backend_config: Mapping[str, Any] | None,
    input_schema: Mapping[str, Any] | None,
    output_schema: Mapping[str, Any] | None,
    side_effect_level: str,
    requires_approval: bool,
    allow_in_preview: bool,
    secret_refs: Sequence[str],
) -> dict[str, Any]:
    """The fields that determine what a tool *does*, not its administrative
    metadata (see module docstring for the full rationale)."""
    return {
        "module_path": module_path,
        "callable_name": callable_name,
        "execution_backend": execution_backend,
        "backend_config": dict(backend_config) if backend_config else None,
        "input_schema": dict(input_schema) if input_schema else None,
        "output_schema": dict(output_schema) if output_schema else None,
        "side_effect_level": side_effect_level,
        "requires_approval": requires_approval,
        "allow_in_preview": allow_in_preview,
        "secret_refs": list(secret_refs),
    }


def _content_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _row_content_sha256(row: CaliberToolRegistry) -> str:
    return _content_sha256(
        _content_payload(
            module_path=row.module_path,
            callable_name=row.callable_name,
            execution_backend=row.execution_backend,
            backend_config=row.backend_config,
            input_schema=row.input_schema,
            output_schema=row.output_schema,
            side_effect_level=row.side_effect_level,
            requires_approval=row.requires_approval,
            allow_in_preview=row.allow_in_preview,
            secret_refs=row.secret_refs or [],
        )
    )


class ToolWorkspaceResourceAdapter:
    """Resolves, snapshots, validates, and releases a CALIBER registered tool."""

    resource_type = "tool"

    # -- resolve / snapshot --------------------------------------------------

    def resolve(
        self, session: object, _workspace: object, declaration: object, identity: object
    ) -> ResolvedToolVersion:
        """Load one exact ``(name, version)`` tool row through the registry's
        own 3-tier visibility model.

        ``declaration`` carries ``resource_id`` (the tool's ``name``) and
        ``version_ref`` (its ``version`` string) -- the same generic shape
        ``routes/workspace.py::snapshot_revision`` builds for every resource
        type. See this module's docstring for why a single visibility-scoped
        query is enough here (no second, external content read is needed the
        way the prompt adapter needs one).
        """
        assert isinstance(session, Session)
        assert isinstance(identity, CaliberIdentity)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("tool declaration must be a mapping")
        name = declaration.get("resource_id") or declaration.get("tool_name")
        raw_version = declaration.get("version_ref", declaration.get("version"))
        if not name or raw_version is None:
            raise WorkspaceReleaseAdapterError(
                "tool declaration requires 'resource_id' and 'version_ref'"
            )
        name = str(name)
        version = str(raw_version).strip()
        if not version:
            raise WorkspaceReleaseAdapterError(
                "tool declaration requires 'resource_id' and 'version_ref'"
            )

        stmt = select(CaliberToolRegistry).where(
            CaliberToolRegistry.name == name, CaliberToolRegistry.version == version
        )
        stmt = apply_visibility_filter(
            stmt, CaliberToolRegistry, identity, identity.active_project_id
        )
        row = session.execute(stmt).scalars().first()
        if row is None:
            raise WorkspaceReleaseAdapterError(
                f"tool {name!r} version {version!r} not found or not visible to the caller"
            )

        return ResolvedToolVersion(
            tool_id=row.tool_id,
            name=row.name,
            version=row.version,
            module_path=row.module_path,
            callable_name=row.callable_name,
            execution_backend=row.execution_backend,
            backend_config=dict(row.backend_config) if row.backend_config else None,
            input_schema=dict(row.input_schema) if row.input_schema else None,
            output_schema=dict(row.output_schema) if row.output_schema else None,
            side_effect_level=row.side_effect_level,
            requires_approval=row.requires_approval,
            allow_in_preview=row.allow_in_preview,
            secret_refs=tuple(row.secret_refs or []),
            status=row.status,
            provider_ref=f"caliber-tool-registry:/{row.tool_id}",
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the tool's definition + safety
        policy (see module docstring for exactly which fields and why)."""
        if not isinstance(resolved_pin, ResolvedToolVersion):
            raise WorkspaceReleaseAdapterError("tool snapshot requires a resolved tool version")
        content_sha256 = _content_sha256(
            _content_payload(
                module_path=resolved_pin.module_path,
                callable_name=resolved_pin.callable_name,
                execution_backend=resolved_pin.execution_backend,
                backend_config=resolved_pin.backend_config,
                input_schema=resolved_pin.input_schema,
                output_schema=resolved_pin.output_schema,
                side_effect_level=resolved_pin.side_effect_level,
                requires_approval=resolved_pin.requires_approval,
                allow_in_preview=resolved_pin.allow_in_preview,
                secret_refs=resolved_pin.secret_refs,
            )
        )
        return SnapshotPin(
            resource_id=resolved_pin.name,
            version_ref=resolved_pin.version,
            content_sha256=content_sha256,
            provider_ref=resolved_pin.provider_ref,
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": TOOL_ADAPTER_VERSION,
                "tool_id": resolved_pin.tool_id,
                "status": resolved_pin.status,
            },
        )

    # -- validate ------------------------------------------------------------

    def validate(
        self,
        session: object,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment | None = None,
    ) -> dict[str, Any]:
        """Not called by any live route or worker today (mirrors
        ``PromptWorkspaceResourceAdapter.validate()``'s own note). Like that
        adapter's ``validate()``, the Protocol's ``validate()`` signature does
        not carry a caller ``identity`` to check the tool's full 3-tier
        visibility model against, so this only compares ``project_id``
        directly -- the same narrower check, and the same class of gap,
        though unreachable in production while nothing calls this method.
        """
        assert isinstance(session, Session)
        name = pin.resource_id
        version = pin.version_ref
        row = (
            session.execute(
                select(CaliberToolRegistry).where(
                    CaliberToolRegistry.name == name, CaliberToolRegistry.version == version
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            raise WorkspaceReleaseAdapterError(f"tool {name!r} version {version!r} not found")
        if (
            row.project_id is not None
            and environment is not None
            and row.project_id != environment.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"tool {name!r} belongs to project {row.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "tool_name": name,
            "version": version,
            "tool_id": row.tool_id,
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
            action="no_op" if before_ref == after_ref else "verify",
            target_ref=_target_ref(pin.resource_id, environment.name),
            before_ref=before_ref,
            after_ref=after_ref,
            metadata={
                "tool_name": pin.resource_id,
                "alias": environment.name,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
                "content_sha256": pin.content_sha256,
            },
        )

    # -- apply / observe / rollback ---------------------------------------
    # A same-database "provider" (see module docstring for why there is
    # nothing to rotate) -- shares the caller's session and only ever reads,
    # exactly like WorkflowWorkspaceResourceAdapter shares its session for a
    # same-database write.

    def apply_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def rollback_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def _verify(self, session: Session, prepared: PreparedAction) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"tool-no-op:{prepared.resource_pin_id}",
                provider_result={"version": prepared.after_ref},
            )
        name, alias = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to verify"
            )
        row = (
            session.execute(
                select(CaliberToolRegistry).where(
                    CaliberToolRegistry.name == name, CaliberToolRegistry.version == after_ref
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return ProviderOutcome(
                "failed",
                f"tool-missing:{name}:{after_ref}",
                error_code="tool_not_found",
                error_summary=f"tool {name!r} version {after_ref!r} no longer exists",
            )
        if row.status == "archived":
            return ProviderOutcome(
                "failed",
                f"tool-archived:{name}:{after_ref}",
                error_code="tool_archived",
                error_summary=f"tool {name!r} version {after_ref!r} has been archived",
            )
        expected_sha256 = prepared.metadata.get("content_sha256")
        live_sha256 = _row_content_sha256(row)
        if expected_sha256 and live_sha256 != expected_sha256:
            return ProviderOutcome(
                "failed",
                f"tool-drift:{name}:{after_ref}",
                error_code="tool_content_drifted",
                error_summary=(
                    f"tool {name!r} version {after_ref!r} content has changed since it was "
                    "pinned; re-snapshot before releasing"
                ),
            )
        return ProviderOutcome(
            "applied",
            f"tool:{name}@{alias}:{after_ref}",
            provider_result={"version": after_ref, "content_sha256": live_sha256},
        )

    # -- observe ---------------------------------------------------------

    def observe_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)


__all__ = ["TOOL_ADAPTER_VERSION", "ResolvedToolVersion", "ToolWorkspaceResourceAdapter"]
