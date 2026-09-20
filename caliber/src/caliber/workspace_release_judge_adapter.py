"""The third real Workspace resource adapter -- a CALIBER judge (`P4-B`/`P4-C`).

``workspace_release_prompt_adapter.py`` was the first adapter whose
``resolve()``/``snapshot()`` are load-bearing (``POST
/projects/{id}/revisions:snapshot``, ``routes/workspace.py::snapshot_revision``).
This module is the second, for a different resource shape: a CALIBER *judge*
(:class:`~caliber.db.models.CaliberJudge`, ``routes/judges.py`` -- "a reusable,
operator-authored scorer: a name, natural-language ``instructions``... and an
optional model"). Judges are selected as scorers in eval runs and rebuilt
deterministically via ``mlflow.genai.make_judge`` at evaluate-time; **CALIBER
itself is the source of truth** (unlike a prompt, there is no external
registry a judge's content lives in).

Versioning -- or the lack of it -- is the key design fork from both existing
adapters, and dictates this module's ``resource_id``/``version_ref`` scheme:

* A prompt is versioned by the MLflow Prompt Registry itself (an external,
  immutable, numbered history) -- ``PromptWorkspaceResourceAdapter`` pins an
  exact integer version.
* A workflow is versioned by CALIBER's own
  :class:`~caliber.db.models.CaliberWorkflowVersion` rows (an internal,
  immutable, numbered history) -- ``WorkflowWorkspaceResourceAdapter`` pins an
  exact ``version_id``.
* A judge has **neither**: :class:`~caliber.db.models.CaliberJudge` is a
  single mutable row with no version table and no append-only history at
  all -- ``routes/judges.py::update_judge`` edits ``instructions``/``model``/
  ``feedback_value_type``/etc. in place. Forcing a fabricated version number
  onto that would misrepresent history that doesn't exist.

``routes/workspace.py::snapshot_revision`` builds its adapter ``declaration``
generically for every resource type (``{"resource_id": ..., "version_ref":
...}``), and the request schema
(``schemas.py::WorkspaceRevisionSnapshotResourceRequest``) requires a
non-empty ``version_ref`` string regardless of resource type. Since a judge
has no numbered version a caller could ask for explicitly, the only truthful
value a caller can supply is the sentinel ``"current"`` (:data:`CURRENT_VERSION_REF`)
-- "pin whatever this judge's live row currently is" -- and ``resolve()``
rejects anything else rather than silently accepting an ID that looks like a
real version but isn't. The *returned* :class:`~caliber.workspace_release_adapters.SnapshotPin`
does **not** echo ``"current"`` back as its own ``version_ref`` -- that would
defeat the whole point of an immutable pin, since "current" means a different
thing every time the judge is edited. Instead ``snapshot()`` sets
``version_ref`` to the judge's own content digest (identical to
``content_sha256``) -- the only stable, reproducible identifier a mutable,
un-versioned single row can offer, and it changes exactly when the judge's
grading behavior changes.

Content basis for ``content_sha256``: ``name``, ``instructions``, ``model``,
and ``feedback_value_type`` -- precisely the fields
``routes/judges.py::build_judge`` reads to reconstruct the judge via
``mlflow.genai.make_judge`` at evaluate-time, i.e. the fields that actually
determine grading behavior. ``description``, ``tags``, and ``status`` are
editorial/lifecycle metadata, not grading logic, so they're excluded --
mirroring the prompt adapter hashing only the loaded template, not its tags.
(Nothing resembling "eval thresholds" is stored on ``CaliberJudge`` itself --
a threshold is a per-request parameter of ``routes/judges.py::align_judge``,
not persisted judge state, so there is nothing there to include.)

Security: ``resolve()`` must check the judge's full 3-tier visibility
(``project``/``user``/``public``) via :func:`caliber.db.scoping.get_visible`
-- the same lesson the prompt adapter's own follow-up fix (PR #393) applied,
reused here from the start rather than reintroduced as a second bug. This is
actually simpler than the prompt case: a prompt's visibility lives on a
*hidden* ``CaliberAgentConfig`` target row that may not even exist (a bare
provider-only prompt is never gated), whereas ``CaliberJudge`` carries
``project_id``/``visibility`` directly on the resource row itself -- the same
row ``get_judge``/``update_judge``/``test_run_judge``/``align_judge`` already
resolve through ``get_visible`` for exactly this reason (see their own
in-route comments about the disclosure a bare lookup would cause). A bare
``judge.project_id`` comparison could not distinguish another user's unshared
personal judge (``project_id=None``, ``visibility="user"``) from a public one
(``project_id=None``, ``visibility="public"``) -- this reuses the real
3-tier check instead of reimplementing that policy.

``apply_release``/``rollback_release``/``observe_release``: a judge has no
per-environment alias or external target to promote at all -- unlike a prompt
(an MLflow alias) or a workflow (a ``caliber_workflow_deployments`` row), an
eval run references a judge directly by its stable name (``Judge.<name>``),
with no environment-scoped indirection in between. There is therefore nothing
external to *mutate* on release. Since the adapter's "provider" is CALIBER's
own database (mirrors ``WorkflowWorkspaceResourceAdapter``'s reasoning for
using the caller's own session rather than a second connection -- see
``workspace_release_adapters.py``'s module docstring on the SQLite
single-writer deadlock this avoids), a judge "release" is instead a genuine
content-integrity check: it re-reads the live row and confirms its content
digest still matches what this revision pinned, catching drift (the judge was
edited out from under the pin via the ordinary ``PATCH /judges/{id}`` route)
rather than silently overwriting the caller's live judge to match a
possibly-stale pin, or fabricating a mutation there is no target for.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberJudge,
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

_TARGET_REF_PREFIX = "judge:"

#: The only value a caller may supply as ``version_ref`` when declaring a
#: judge pin -- see this module's docstring for why a judge has no numbered
#: version to be explicit about instead.
CURRENT_VERSION_REF = "current"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention
#: ``workspace_release_prompt_adapter.py::PROMPT_ADAPTER_VERSION`` uses.
JUDGE_ADAPTER_VERSION = "workspace-release-judge-adapter/1"


@dataclass(frozen=True)
class ResolvedJudge:
    """A CALIBER judge, loaded and visibility-checked."""

    judge_id: str
    name: str
    instructions: str
    model: str | None
    feedback_value_type: str | None


def _target_ref(judge_id: str) -> str:
    return f"{_TARGET_REF_PREFIX}{judge_id}"


def _parse_target_ref(target_ref: str) -> str:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or len(target_ref) <= len(_TARGET_REF_PREFIX):
        raise WorkspaceReleaseAdapterError(f"malformed judge target_ref {target_ref!r}")
    return target_ref[len(_TARGET_REF_PREFIX) :]


def _content_sha256(
    name: str, instructions: str, model: str | None, feedback_value_type: str | None
) -> str:
    """The single hashing routine both ``snapshot()`` and the release-time
    drift check use, so a pinned digest and a freshly recomputed one are
    always comparable -- never two subtly different canonicalizations."""
    payload: dict[str, Any] = {
        "name": name,
        "instructions": instructions,
        "model": model,
        "feedback_value_type": feedback_value_type,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class JudgeWorkspaceResourceAdapter:
    """Resolves, snapshots, validates, and releases a CALIBER judge."""

    resource_type = "judge"

    # -- resolve / snapshot --------------------------------------------------

    def resolve(
        self, session: object, _workspace: object, declaration: object, identity: object
    ) -> ResolvedJudge:
        """Load a judge and enforce its own 3-tier visibility model.

        ``declaration`` carries ``resource_id`` (the judge's ``judge_id``) and
        ``version_ref``, which must be exactly :data:`CURRENT_VERSION_REF` --
        see this module's docstring for why a judge has no numbered version a
        caller could ask for instead.
        """
        assert isinstance(session, Session)
        assert isinstance(identity, CaliberIdentity)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("judge declaration must be a mapping")
        judge_id = declaration.get("resource_id") or declaration.get("judge_id")
        version_ref = declaration.get("version_ref")
        if not judge_id or not version_ref:
            raise WorkspaceReleaseAdapterError(
                "judge declaration requires 'resource_id' and 'version_ref'"
            )
        judge_id = str(judge_id)
        if str(version_ref) != CURRENT_VERSION_REF:
            raise WorkspaceReleaseAdapterError(
                f"judge version_ref must be {CURRENT_VERSION_REF!r} (a judge has no "
                f"numbered version history to pin an explicit version of); got {version_ref!r}"
            )

        judge = get_visible(session, CaliberJudge, CaliberJudge.judge_id, judge_id, identity)
        if judge is None:
            # Matches ``routes/judges.py::get_judge``'s own "not found" 404 for
            # both a genuinely missing judge and one that exists but is not
            # visible to this caller -- never distinguishing the two, so this
            # can't be used to probe for another user's judge ids.
            raise WorkspaceReleaseAdapterError(f"judge {judge_id!r} not found or not visible")

        return ResolvedJudge(
            judge_id=judge.judge_id,
            name=judge.name,
            instructions=judge.instructions,
            model=judge.model,
            feedback_value_type=judge.feedback_value_type,
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the judge's grading fields.

        Unlike ``WorkflowWorkspaceResourceAdapter.snapshot()`` (still an
        explicit placeholder), this is real -- see this module's docstring
        for why ``version_ref`` is set to the digest itself rather than a
        fabricated version number.
        """
        if not isinstance(resolved_pin, ResolvedJudge):
            raise WorkspaceReleaseAdapterError("judge snapshot requires a resolved judge")
        content_sha256 = _content_sha256(
            resolved_pin.name,
            resolved_pin.instructions,
            resolved_pin.model,
            resolved_pin.feedback_value_type,
        )
        return SnapshotPin(
            resource_id=resolved_pin.judge_id,
            version_ref=content_sha256,
            content_sha256=content_sha256,
            provider_ref=f"judge:{resolved_pin.judge_id}",
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": JUDGE_ADAPTER_VERSION,
                "instructions_length": len(resolved_pin.instructions),
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
        ``PromptWorkspaceResourceAdapter.validate()``'s own note). The
        :class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
        Protocol's ``validate()`` does not carry a caller ``identity``, so
        this only compares ``project_id`` directly, the same narrower check
        (and the same class of gap) ``resolve()`` above closes with a real
        identity -- unreachable in production while nothing calls this
        method. Widening ``validate()``'s Protocol signature the same way is
        left for when a real caller needs it.
        """
        assert isinstance(session, Session)
        judge_id = pin.resource_id
        judge = session.get(CaliberJudge, judge_id)
        if judge is None:
            raise WorkspaceReleaseAdapterError(f"judge {judge_id!r} not found")
        if (
            judge.project_id is not None
            and environment is not None
            and judge.project_id != environment.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"judge {judge_id!r} belongs to project {judge.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "judge_id": judge_id,
            "content_sha256": pin.content_sha256,
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
                "judge_id": pin.resource_id,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
            },
        )

    # -- apply / observe / rollback ---------------------------------------
    # A genuinely same-database provider (mirrors
    # WorkflowWorkspaceResourceAdapter's reasoning) -- all three share the
    # caller's session and only ever read, since there is no external alias
    # or target row to write for a judge. See this module's docstring for
    # why a judge "release" is a content-integrity check, not a mutation.

    def apply_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def rollback_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def observe_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def _verify(self, session: Session, prepared: PreparedAction) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"judge-no-op:{prepared.resource_pin_id}",
                provider_result={"content_sha256": prepared.after_ref},
            )
        judge_id = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to verify"
            )
        judge = session.get(CaliberJudge, judge_id)
        if judge is None:
            return ProviderOutcome(
                "failed",
                f"judge-missing:{judge_id}",
                error_code="judge_not_found",
                error_summary=f"judge {judge_id!r} not found",
            )
        project_id = prepared.metadata.get("project_id")
        if (
            project_id is not None
            and judge.project_id is not None
            and judge.project_id != project_id
        ):
            return ProviderOutcome(
                "failed",
                f"judge-tenancy:{judge_id}",
                error_code="resource_outside_project",
                error_summary=(
                    f"judge {judge_id!r} belongs to project {judge.project_id!r}, "
                    f"not release project {project_id!r}"
                ),
            )
        current_sha256 = _content_sha256(
            judge.name, judge.instructions, judge.model, judge.feedback_value_type
        )
        if current_sha256 != after_ref:
            return ProviderOutcome(
                "failed",
                f"judge-drift:{judge_id}",
                error_code="judge_content_drifted",
                error_summary=(
                    f"judge {judge_id!r} has changed since it was pinned "
                    f"(pinned {after_ref!r}, live {current_sha256!r})"
                ),
            )
        return ProviderOutcome(
            "applied",
            f"judge:{judge_id}:{current_sha256[:12]}",
            provider_result={"content_sha256": current_sha256},
        )


__all__ = [
    "CURRENT_VERSION_REF",
    "JUDGE_ADAPTER_VERSION",
    "JudgeWorkspaceResourceAdapter",
    "ResolvedJudge",
]
