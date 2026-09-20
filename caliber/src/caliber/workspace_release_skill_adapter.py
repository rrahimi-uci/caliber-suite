"""The fourth real Workspace resource adapter -- a CALIBER skill (`P4-B`/`P4-C`,
slice 4 of the multi-slice managed-snapshot epic).

``workspace_release_prompt_adapter.py``, ``workspace_release_tool_adapter.py``,
and ``workspace_release_judge_adapter.py`` each proved this Protocol against a
different resource shape:

* A prompt has **no** CALIBER-side content at all -- content lives in an
  external, immutable, numbered registry (MLflow), so ``resolve()`` needs two
  lookups (a CALIBER-side visibility check, then a genuinely external read).
* A tool is a single mutable ``(name, version)`` row that is *simultaneously*
  the identity, the visibility record, and the content store -- one
  visibility-filtered query, no version history at all.
* A judge is a single mutable row with **no version identity whatsoever** --
  no numbered history whether external or internal -- so its adapter accepts
  only the sentinel ``version_ref="current"`` and mints a content digest as
  the pin's own reproducible version identity.

A :class:`~caliber.db.models.CaliberSkill` (``routes/skills.py``) is a fourth,
genuinely different shape: **both** a mutable live row *and* a real, internal,
immutable, numbered content history --
:class:`~caliber.db.models.CaliberSkillVersion` (``caliber_skill_versions``,
written by :mod:`caliber.skill_versions` on every create/update/rollback in
the same transaction as the live-row mutation). Unlike a tool or judge, a
skill genuinely has a numbered version identity a caller can pin explicitly
(mirroring the prompt adapter's reproducibility stance: never resolve
"current" into a version, because that would make the same snapshot request
non-reproducible depending on when it runs) -- but unlike a prompt, that
history lives in CALIBER's own database, not an external registry, so there
is no second system to call out to. ``resolve()`` here is therefore two
lookups against the *same* database rather than one lookup each against two
different systems:

1. A visibility check against the live ``CaliberSkill`` row via
   :func:`caliber.db.scoping.get_visible` -- the mandatory, non-negotiable
   fix the prompt adapter needed a follow-up PR (#393) to add, applied here
   from the very first implementation. ``CaliberSkill`` carries
   ``project_id``/``visibility``/``owner`` directly on the resource row
   itself (the same shape ``tool``/``judge`` use), so this is exactly the
   check ``routes/skills.py::_visible_skill_or_404`` already applies to every
   skill route -- reused, not reimplemented. A bare
   ``skill.project_id != caller's project`` comparison cannot distinguish a
   public skill from another user's unshared personal one when both have
   ``project_id is None``, so this never takes that shortcut.
2. An exact ``(skill_id, version_number)`` read against
   ``caliber_skill_versions`` for the pinned version's immutable ``content``
   and ``summary`` -- the two fields
   :func:`caliber.skill_versions.record_skill_version` snapshots per version
   and the two fields that make up a skill's progressive-disclosure content
   (level 1 "when to activate" / level 2 "what to do"). Only these two fields
   are ever captured in history -- ``name``/``category``/``tags``/
   ``allowed_tools``/``depends_on`` live only on the mutable live row and are
   never historized -- so they are deliberately excluded from
   ``content_sha256`` (mirroring the prompt adapter hashing only the loaded
   template, not its tags): including a value that can silently change
   without a version bump would make an "immutable" pin not actually
   reproducible, and would make the release-time drift check below compare
   two different things.

Pin field convention (mirroring the three adapters above where it fits):

* ``resource_id`` -- the skill's ``skill_id`` (its stable, URL-facing
  surrogate key -- see ``CaliberSkill``'s own docstring, "the surrogate
  primary key used in URLs" -- mirroring the judge adapter's choice of
  ``judge_id`` over the also-unique ``name``).
* ``version_ref`` -- the exact, caller-supplied integer ``version_number``,
  as a string (mirroring the prompt adapter's explicit-version-only stance),
  *not* a content digest -- unlike a judge, a skill has a genuine version
  identity there is no reason to paper over.
* ``provider_ref`` -- a synthetic ``caliber-skill-version:/{skill_id}/{version_number}``
  reference recording the exact history row read, the same audit-trail
  convention the tool adapter's synthetic ``caliber-tool-registry:/...`` ref
  establishes for a same-database "provider".

``apply_release``/``rollback_release``/``observe_release``: like a tool or
judge, a skill has no per-environment alias or external deployment target to
promote at all -- an agent composes a referenced skill directly by ``name``
(``routes/agents.py::_extract_skill_refs``), with no environment-scoped
indirection in between. This adapter's "provider" is therefore this same
CALIBER database (mirrors ``workspace_release_tool_adapter.py``/
``workspace_release_judge_adapter.py``'s reasoning for sharing the caller's
own session rather than opening a second connection -- see
``workspace_release_adapters.py``'s module docstring on the SQLite
single-writer deadlock that avoids), and a release is a content-integrity
*verification*, never a mutation -- ``action`` is ``"verify"``, not
``"promote"``. Although the pinned ``(skill_id, version_number)`` content
row is, by construction, immutable once written (``uq_skill_version_number``
never permits a second row at the same version number), release-time
verification still re-reads the live ``CaliberSkill`` row and the pinned
history row and fails closed rather than assuming: it refuses a
since-archived skill (``skill_archived``, mirroring ``tool_archived``), a
since-reassigned project (``resource_outside_project``, mirroring the judge
adapter), a history row that has since disappeared (``skill_version_not_found``,
a defensive check with no equivalent in the immutable-by-registry prompt
case), and a recomputed content digest that no longer matches the pin
(``skill_content_drifted``, defense in depth against any of the above rather
than an expected steady-state occurrence).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberSkill,
    CaliberSkillVersion,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.db.scoping import get_visible
from caliber.workflows.manifest import canonical_json
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)

logger = logging.getLogger(__name__)

_TARGET_REF_PREFIX = "skill:"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention
#: ``workspace_release_prompt_adapter.py::PROMPT_ADAPTER_VERSION`` uses.
SKILL_ADAPTER_VERSION = "workspace-release-skill-adapter/1"


@dataclass(frozen=True)
class ResolvedSkillVersion:
    """One immutable ``caliber_skill_versions`` row, loaded and
    visibility-checked against its live parent ``CaliberSkill``."""

    skill_id: str
    name: str
    version_number: int
    content: str
    summary: str
    provider_ref: str


def _target_ref(skill_id: str) -> str:
    return f"{_TARGET_REF_PREFIX}{skill_id}"


def _parse_target_ref(target_ref: str) -> str:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or len(target_ref) <= len(_TARGET_REF_PREFIX):
        raise WorkspaceReleaseAdapterError(f"malformed skill target_ref {target_ref!r}")
    return target_ref[len(_TARGET_REF_PREFIX) :]


def _content_payload(content: str, summary: str) -> dict[str, Any]:
    """The two fields ``caliber_skill_versions`` captures per version -- see
    module docstring for why nothing else (``name``, ``category``, ``tags``,
    ``allowed_tools``, ``depends_on``) belongs here."""
    return {"content": content, "summary": summary}


def _content_sha256(content: str, summary: str) -> str:
    """The single hashing routine both ``snapshot()`` and the release-time
    drift check use, so a pinned digest and a freshly recomputed one are
    always comparable -- never two subtly different canonicalizations."""
    return hashlib.sha256(
        canonical_json(_content_payload(content, summary)).encode("utf-8")
    ).hexdigest()


class SkillWorkspaceResourceAdapter:
    """Resolves, snapshots, validates, and releases a CALIBER skill."""

    resource_type = "skill"

    # -- resolve / snapshot --------------------------------------------------

    def resolve(
        self, session: object, _workspace: object, declaration: object, identity: object
    ) -> ResolvedSkillVersion:
        """Load one exact skill version through the live skill's own 3-tier
        visibility model, then its immutable content snapshot.

        ``declaration`` carries ``resource_id`` (the skill's ``skill_id``)
        and ``version_ref`` (the exact ``caliber_skill_versions.version_number``,
        as a string/int) -- the same generic shape
        ``routes/workspace.py::snapshot_revision`` builds for every resource
        type. See this module's docstring for why a skill -- unlike a judge --
        has a real version identity to be explicit about, and why the
        visibility check runs against the live row while the content read
        runs against the separate immutable history table.
        """
        assert isinstance(session, Session)
        assert isinstance(identity, CaliberIdentity)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("skill declaration must be a mapping")
        skill_id = declaration.get("resource_id") or declaration.get("skill_id")
        raw_version = declaration.get("version_ref", declaration.get("version"))
        if not skill_id or raw_version is None:
            raise WorkspaceReleaseAdapterError(
                "skill declaration requires 'resource_id' and 'version_ref'"
            )
        skill_id = str(skill_id)
        try:
            version_number = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise WorkspaceReleaseAdapterError(
                f"skill version {raw_version!r} must be an integer"
            ) from exc

        skill = get_visible(session, CaliberSkill, CaliberSkill.skill_id, skill_id, identity)
        if skill is None:
            # Matches ``routes/skills.py::_visible_skill_or_404``'s own "not
            # found" 404 for both a genuinely missing skill and one that
            # exists but is not visible to this caller -- never distinguishing
            # the two, so this can't be used to probe for another user's
            # skill ids.
            raise WorkspaceReleaseAdapterError(
                f"skill {skill_id!r} not found or not visible to the caller"
            )

        version_row = (
            session.execute(
                select(CaliberSkillVersion).where(
                    CaliberSkillVersion.skill_id == skill_id,
                    CaliberSkillVersion.version_number == version_number,
                )
            )
            .scalars()
            .first()
        )
        if version_row is None:
            raise WorkspaceReleaseAdapterError(
                f"skill {skill_id!r} has no recorded version {version_number!r} "
                "in its content history"
            )

        return ResolvedSkillVersion(
            skill_id=skill.skill_id,
            name=skill.name,
            version_number=version_number,
            content=version_row.content,
            summary=version_row.summary,
            provider_ref=f"caliber-skill-version:/{skill_id}/{version_number}",
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the skill's versioned
        content + summary (see module docstring for exactly which fields and
        why)."""
        if not isinstance(resolved_pin, ResolvedSkillVersion):
            raise WorkspaceReleaseAdapterError("skill snapshot requires a resolved skill version")
        content_sha256 = _content_sha256(resolved_pin.content, resolved_pin.summary)
        return SnapshotPin(
            resource_id=resolved_pin.skill_id,
            version_ref=str(resolved_pin.version_number),
            content_sha256=content_sha256,
            provider_ref=resolved_pin.provider_ref,
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": SKILL_ADAPTER_VERSION,
                "content_length": len(resolved_pin.content),
                "summary_length": len(resolved_pin.summary),
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
        not carry a caller ``identity`` to check the skill's full 3-tier
        visibility model against, so this only compares ``project_id``
        directly -- the same narrower check, and the same class of gap,
        though unreachable in production while nothing calls this method.
        """
        assert isinstance(session, Session)
        skill_id = pin.resource_id
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            raise WorkspaceReleaseAdapterError(f"skill {skill_id!r} not found")
        if (
            skill.project_id is not None
            and environment is not None
            and skill.project_id != environment.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"skill {skill_id!r} belongs to project {skill.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        try:
            version_number = int(pin.version_ref)
        except (TypeError, ValueError) as exc:
            raise WorkspaceReleaseAdapterError(
                f"skill {skill_id!r} pin has a non-integer version {pin.version_ref!r}"
            ) from exc
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "skill_id": skill_id,
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
            action="no_op" if before_ref == after_ref else "verify",
            target_ref=_target_ref(pin.resource_id),
            before_ref=before_ref,
            after_ref=after_ref,
            metadata={
                "skill_id": pin.resource_id,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
                "content_sha256": pin.content_sha256,
            },
        )

    # -- apply / observe / rollback ---------------------------------------
    # A same-database "provider" (see module docstring for why there is
    # nothing to rotate) -- all three share the caller's session and only
    # ever read, exactly like ToolWorkspaceResourceAdapter/
    # JudgeWorkspaceResourceAdapter.

    def apply_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def rollback_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def observe_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def _verify(  # noqa: PLR0911 (one return per failure mode reads clearer than nesting)
        self, session: Session, prepared: PreparedAction
    ) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"skill-no-op:{prepared.resource_pin_id}",
                provider_result={"version": prepared.after_ref},
            )
        skill_id = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to verify"
            )
        skill = session.get(CaliberSkill, skill_id)
        if skill is None:
            return ProviderOutcome(
                "failed",
                f"skill-missing:{skill_id}",
                error_code="skill_not_found",
                error_summary=f"skill {skill_id!r} not found",
            )
        if skill.status == "archived":
            return ProviderOutcome(
                "failed",
                f"skill-archived:{skill_id}",
                error_code="skill_archived",
                error_summary=f"skill {skill_id!r} has been archived",
            )
        project_id = prepared.metadata.get("project_id")
        if (
            project_id is not None
            and skill.project_id is not None
            and skill.project_id != project_id
        ):
            return ProviderOutcome(
                "failed",
                f"skill-tenancy:{skill_id}",
                error_code="resource_outside_project",
                error_summary=(
                    f"skill {skill_id!r} belongs to project {skill.project_id!r}, "
                    f"not release project {project_id!r}"
                ),
            )
        try:
            version_number = int(after_ref)
        except (TypeError, ValueError):
            return ProviderOutcome(
                "failed",
                f"skill-invalid-version:{skill_id}",
                error_code="invalid_skill_version",
                error_summary=f"after_ref {after_ref!r} is not an integer skill version",
            )
        version_row = (
            session.execute(
                select(CaliberSkillVersion).where(
                    CaliberSkillVersion.skill_id == skill_id,
                    CaliberSkillVersion.version_number == version_number,
                )
            )
            .scalars()
            .first()
        )
        if version_row is None:
            return ProviderOutcome(
                "failed",
                f"skill-version-missing:{skill_id}:{version_number}",
                error_code="skill_version_not_found",
                error_summary=(
                    f"skill {skill_id!r} version {version_number!r} no longer exists "
                    "in its content history"
                ),
            )
        expected_sha256 = prepared.metadata.get("content_sha256")
        live_sha256 = _content_sha256(version_row.content, version_row.summary)
        if expected_sha256 and live_sha256 != expected_sha256:
            return ProviderOutcome(
                "failed",
                f"skill-drift:{skill_id}:{version_number}",
                error_code="skill_content_drifted",
                error_summary=(
                    f"skill {skill_id!r} version {version_number!r} content has changed "
                    "since it was pinned; re-snapshot before releasing"
                ),
            )
        return ProviderOutcome(
            "applied",
            f"skill:{skill_id}:{version_number}",
            provider_result={"version": version_number, "content_sha256": live_sha256},
        )


__all__ = ["SKILL_ADAPTER_VERSION", "ResolvedSkillVersion", "SkillWorkspaceResourceAdapter"]
