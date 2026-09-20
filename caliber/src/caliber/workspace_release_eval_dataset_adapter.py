"""The fourth real Workspace resource adapter -- a CALIBER eval dataset
(`P4-B`/`P4-C`, slice 4 of the multi-slice managed-snapshot epic).

``workspace_release_prompt_adapter.py`` (external-registry-versioned),
``workspace_release_tool_adapter.py`` (a single mutable row, unique on
``(name, version)``), and ``workspace_release_judge_adapter.py`` (a single
mutable row with **no** version concept at all) cover three distinct
resource shapes. An eval dataset (:class:`~caliber.db.models.CaliberEvalDataset`,
"test set" in some route docstrings, ``routes/eval_datasets.py``) is a
**fourth, genuinely different shape**: a *collection* resource. The dataset
row itself is metadata (``name``/``description``/``owner``/``tags``/
``status``) plus an integer ``version`` counter; the actual content an eval
run scores against -- the example set -- lives in a related, append-only
child table, :class:`~caliber.db.models.CaliberEvalDatasetExample`.

Versioning -- read carefully, this is *not* the judge case, despite both
being CALIBER-native (no external registry):

* Unlike a judge, an eval dataset **does** have a real, caller-meaningful
  numbered version history: ``CaliberEvalDataset.version`` increments on
  every example mutation (``routes/eval_datasets.py::create_example``/
  ``create_example_from_trace``/``supersede_example``/``revise_example``/
  ``restore_dataset_version``), and -- critically -- that version number is
  already used elsewhere in this codebase as an explicit, reproducible pin:
  ``routes/evaluations.py::_load_example_rows`` reconstructs "the example
  set active as of version N" for a pinned eval run, and
  ``routes/eval_datasets.py::list_examples``'s own ``as_of_version`` query
  param does the same for a preview. That reconstruction is stable forever
  once N has passed: an example's ``dataset_version`` (when it was added)
  and ``superseded_version`` (when it was retired, set at most once) are
  write-once fields, so "the set active as of version N" for any N that has
  already occurred can never change again -- the same reproducibility
  guarantee an immutable MLflow prompt version or a
  :class:`~caliber.db.models.CaliberWorkflowVersion` row gives, just
  reconstructed from a child table's rows instead of read from a single
  immutable row. So this adapter reuses that existing, already-relied-upon
  version scheme directly: ``resolve()`` requires an explicit
  caller-supplied integer ``version_ref`` (never "current"/an alias, for the
  same non-reproducibility reason ``workspace_release_prompt_adapter.py``'s
  own docstring gives -- and unlike the judge adapter, there genuinely is a
  real version number here to be explicit about, so the judge's
  ``CURRENT_VERSION_REF`` sentinel pattern does not apply).

Content basis for ``content_sha256`` -- and the reason this adapter cannot
just hash the ``CaliberEvalDataset`` row the way the judge adapter hashes
``CaliberJudge``: the dataset row's own columns (``name``/``description``/
``tags``/``status``) are administrative, not the reproducible eval input.
"The actual content" a release is pinning is the example set active as of
the requested version -- a genuine collection digest, not a single row's
fields. ``snapshot()`` therefore hashes an ordered list of
``{example_id, input, expected, weight, tags}`` for every example
reconstructed "as of version N" (the exact predicate and field set
``routes/evaluations.py::_load_example_rows`` already uses, reimplemented
here rather than imported from a route module -- mirrors every other
adapter in this package staying self-contained/route-independent).
``example_id`` is included (not just ``input``/``expected``/``weight``/
``tags``) so that swapping one example for a content-identical one with a
different id -- an edge case, but a real one, since
``revise_example`` always mints a fresh id even if an operator "revises" a
row back to its original content -- still changes the digest, matching this
adapter's whole purpose: adding, removing, or editing *any* example must
change ``content_sha256``. Examples are ordered by
``(created_at, example_id)`` -- the same ordering
``routes/eval_datasets.py::list_examples`` uses, with ``example_id`` as an
explicit tie-break for full determinism under coarse timestamp resolution
(two examples inserted in the same instant must still hash the same way
every time). ``version_ref`` is the pinned integer version itself (as a
string), *not* a digest -- unlike the judge adapter (which has no version
number to echo back), an eval dataset's version is already the caller's own
reproducible identifier, so there is nothing to substitute it with.

Security: ``resolve()`` checks the dataset's own 3-tier visibility
(``project``/``user``/``public``) via :func:`caliber.db.scoping.get_visible`
-- the same call ``routes/eval_datasets.py::get_dataset``/``list_examples``
already use for every dataset *lookup* route, and the same lesson the prompt
adapter's PR #393 follow-up fix established: a bare ``dataset.project_id !=
caller's project`` comparison cannot distinguish a public dataset from
another user's unshared personal one (both read ``project_id IS NULL``).
Applied from the start here, not as a follow-up fix.

``apply_release``/``rollback_release``/``observe_release``: like a judge (and
unlike a prompt or workflow), an eval dataset has no per-environment alias or
external target to promote -- a pinned eval run references a dataset
directly by ``dataset_id`` + explicit version, with no environment-scoped
indirection in between. The adapter's "provider" is therefore CALIBER's own
database (mirrors :mod:`caliber.workspace_release_judge_adapter`'s
reasoning), so all three share the caller's session and only ever read. What
they verify: the dataset still exists and is not archived (an eval dataset
row can gain ``status="archived"`` via ``routes/eval_datasets.py::update_dataset``,
the collection-resource analogue of a tool's ``status="archived"`` check --
mirrors ``workspace_release_tool_adapter.py``'s ``tool_archived`` refusal),
that the pinned version still exists (``version <= dataset.version`` --
defensive; monotonic version growth means this should never regress, but a
future direct-DB reset or migration bug should not silently pass), and that
the reconstructed "as of version N" example set still hashes to the pin's
``content_sha256``. Unlike a tool or judge row (both directly mutable
in-place via their own ``PATCH`` routes, so drift is an expected, reachable
condition), a genuine content mismatch here should be *impossible* given the
append-only/write-once guarantees above -- this check is deliberately kept
anyway as defense-in-depth (catching a hypothetical future edit-in-place
route, direct database tampering, or a bug in the reconstruction predicate
itself) rather than assumed away, the same "never trust a stale pin without
re-checking" posture every other adapter in this package takes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
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

_TARGET_REF_PREFIX = "eval_dataset:"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention
#: ``workspace_release_prompt_adapter.py::PROMPT_ADAPTER_VERSION`` uses.
EVAL_DATASET_ADAPTER_VERSION = "workspace-release-eval-dataset-adapter/1"


@dataclass(frozen=True)
class ResolvedEvalDataset:
    """An eval dataset's "as of version N" example set, loaded and
    visibility-checked."""

    dataset_id: str
    name: str
    version: int
    examples: tuple[Mapping[str, Any], ...]
    provider_ref: str


def _target_ref(dataset_id: str) -> str:
    return f"{_TARGET_REF_PREFIX}{dataset_id}"


def _parse_target_ref(target_ref: str) -> str:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or len(target_ref) <= len(_TARGET_REF_PREFIX):
        raise WorkspaceReleaseAdapterError(f"malformed eval_dataset target_ref {target_ref!r}")
    return target_ref[len(_TARGET_REF_PREFIX) :]


def _examples_as_of(
    session: Session, dataset_id: str, version: int
) -> tuple[Mapping[str, Any], ...]:
    """Reconstruct the example set active "as of version N" -- the same
    predicate ``routes/evaluations.py::_load_example_rows`` and
    ``routes/eval_datasets.py::list_examples``'s ``as_of_version`` filter
    both use: include rows added at or before N, exclude rows retired at or
    before N. Deliberately reimplemented rather than imported from a route
    module (see this module's docstring)."""
    stmt = (
        select(CaliberEvalDatasetExample)
        .where(CaliberEvalDatasetExample.dataset_id == dataset_id)
        .where(CaliberEvalDatasetExample.dataset_version <= version)
        .where(
            or_(
                CaliberEvalDatasetExample.superseded_version.is_(None),
                CaliberEvalDatasetExample.superseded_version > version,
            )
        )
        .order_by(
            CaliberEvalDatasetExample.created_at,
            CaliberEvalDatasetExample.example_id,
        )
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(
        {
            "example_id": row.example_id,
            "input": dict(row.input or {}),
            "expected": dict(row.expected or {}),
            "weight": row.weight if row.weight is not None else 1.0,
            "tags": list(row.tags or []),
        }
        for row in rows
    )


def _content_sha256(examples: Sequence[Mapping[str, Any]]) -> str:
    """The single hashing routine both ``snapshot()`` and the release-time
    drift check use, so a pinned digest and a freshly recomputed one are
    always comparable -- never two subtly different canonicalizations."""
    payload: dict[str, Any] = {"examples": [dict(example) for example in examples]}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class EvalDatasetWorkspaceResourceAdapter:
    """Resolves, snapshots, validates, and releases a CALIBER eval dataset."""

    resource_type = "eval_dataset"

    # -- resolve / snapshot --------------------------------------------------

    def resolve(
        self, session: object, _workspace: object, declaration: object, identity: object
    ) -> ResolvedEvalDataset:
        """Load one dataset and reconstruct its example set "as of" an
        explicit, caller-supplied integer version, enforcing the dataset's
        own 3-tier visibility model.

        ``declaration`` carries ``resource_id`` (the dataset's ``dataset_id``)
        and ``version_ref`` (an explicit integer version, as a string/int) --
        see this module's docstring for why an eval dataset, unlike a judge,
        has a real version number a caller can and must be explicit about.
        """
        assert isinstance(session, Session)
        assert isinstance(identity, CaliberIdentity)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("eval_dataset declaration must be a mapping")
        dataset_id = declaration.get("resource_id") or declaration.get("dataset_id")
        raw_version = declaration.get("version_ref", declaration.get("version"))
        if not dataset_id or raw_version is None:
            raise WorkspaceReleaseAdapterError(
                "eval_dataset declaration requires 'resource_id' and 'version_ref'"
            )
        dataset_id = str(dataset_id)
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise WorkspaceReleaseAdapterError(
                f"eval_dataset version {raw_version!r} must be an integer"
            ) from exc
        if version < 1:
            raise WorkspaceReleaseAdapterError(
                f"eval_dataset version {version} must be a positive integer"
            )

        dataset = get_visible(
            session, CaliberEvalDataset, CaliberEvalDataset.dataset_id, dataset_id, identity
        )
        if dataset is None:
            # Matches routes/eval_datasets.py::get_dataset's own "not found"
            # 404 for both a genuinely missing dataset and one that exists
            # but is not visible to this caller -- never distinguishing the
            # two, so this can't be used to probe for another user's dataset
            # ids.
            raise WorkspaceReleaseAdapterError(
                f"eval dataset {dataset_id!r} not found or not visible to the caller"
            )
        if version > dataset.version:
            raise WorkspaceReleaseAdapterError(
                f"eval dataset {dataset_id!r} has no version {version} "
                f"(current version is {dataset.version})"
            )

        examples = _examples_as_of(session, dataset.dataset_id, version)
        return ResolvedEvalDataset(
            dataset_id=dataset.dataset_id,
            name=dataset.name,
            version=version,
            examples=examples,
            provider_ref=f"caliber-eval-dataset:/{dataset.dataset_id}@{version}",
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the reconstructed example
        set (see module docstring for exactly which fields and why)."""
        if not isinstance(resolved_pin, ResolvedEvalDataset):
            raise WorkspaceReleaseAdapterError(
                "eval_dataset snapshot requires a resolved eval dataset"
            )
        content_sha256 = _content_sha256(resolved_pin.examples)
        return SnapshotPin(
            resource_id=resolved_pin.dataset_id,
            version_ref=str(resolved_pin.version),
            content_sha256=content_sha256,
            provider_ref=resolved_pin.provider_ref,
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": EVAL_DATASET_ADAPTER_VERSION,
                "example_count": len(resolved_pin.examples),
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
        ``JudgeWorkspaceResourceAdapter.validate()``'s own note). The
        :class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
        Protocol's ``validate()`` does not carry a caller ``identity``, so
        this only compares ``project_id`` directly -- the same narrower
        check (and the same class of gap) ``resolve()`` above closes with a
        real identity -- unreachable in production while nothing calls this
        method.
        """
        assert isinstance(session, Session)
        dataset_id = pin.resource_id
        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            raise WorkspaceReleaseAdapterError(f"eval dataset {dataset_id!r} not found")
        if (
            dataset.project_id is not None
            and environment is not None
            and dataset.project_id != environment.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"eval dataset {dataset_id!r} belongs to project {dataset.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        try:
            version = int(pin.version_ref)
        except (TypeError, ValueError) as exc:
            raise WorkspaceReleaseAdapterError(
                f"eval dataset {dataset_id!r} pin has a non-integer version {pin.version_ref!r}"
            ) from exc
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "dataset_id": dataset_id,
            "version": version,
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
                "dataset_id": pin.resource_id,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
                "content_sha256": pin.content_sha256,
            },
        )

    # -- apply / observe / rollback ---------------------------------------
    # A same-database "provider" (see module docstring for why there is
    # nothing to rotate) -- shares the caller's session and only ever reads,
    # exactly like JudgeWorkspaceResourceAdapter/ToolWorkspaceResourceAdapter.

    def apply_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def rollback_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def observe_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    # Explicit multi-condition verification, mirroring
    # workspace_release_operations.py::apply_workspace_release_operation's own
    # lint suppression below for the same "many distinct, individually-named
    # failure reasons" shape.
    def _verify(  # noqa: PLR0911
        self, session: Session, prepared: PreparedAction
    ) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"eval-dataset-no-op:{prepared.resource_pin_id}",
                provider_result={"version": prepared.after_ref},
            )
        dataset_id = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to verify"
            )
        try:
            version = int(after_ref)
        except (TypeError, ValueError):
            return ProviderOutcome(
                "failed",
                f"eval-dataset-invalid-version:{dataset_id}",
                error_code="invalid_eval_dataset_version",
                error_summary=f"after_ref {after_ref!r} is not an integer eval dataset version",
            )

        dataset = session.get(CaliberEvalDataset, dataset_id)
        if dataset is None:
            return ProviderOutcome(
                "failed",
                f"eval-dataset-missing:{dataset_id}",
                error_code="eval_dataset_not_found",
                error_summary=f"eval dataset {dataset_id!r} no longer exists",
            )
        if dataset.status == "archived":
            return ProviderOutcome(
                "failed",
                f"eval-dataset-archived:{dataset_id}",
                error_code="eval_dataset_archived",
                error_summary=f"eval dataset {dataset_id!r} has been archived",
            )
        project_id = prepared.metadata.get("project_id")
        if (
            project_id is not None
            and dataset.project_id is not None
            and dataset.project_id != project_id
        ):
            return ProviderOutcome(
                "failed",
                f"eval-dataset-tenancy:{dataset_id}",
                error_code="resource_outside_project",
                error_summary=(
                    f"eval dataset {dataset_id!r} belongs to project {dataset.project_id!r}, "
                    f"not release project {project_id!r}"
                ),
            )
        if version > dataset.version:
            return ProviderOutcome(
                "failed",
                f"eval-dataset-version-missing:{dataset_id}:{version}",
                error_code="eval_dataset_version_not_found",
                error_summary=(
                    f"eval dataset {dataset_id!r} has no version {version} "
                    f"(current version is {dataset.version})"
                ),
            )

        examples = _examples_as_of(session, dataset_id, version)
        live_sha256 = _content_sha256(examples)
        expected_sha256 = prepared.metadata.get("content_sha256")
        if expected_sha256 and live_sha256 != expected_sha256:
            return ProviderOutcome(
                "failed",
                f"eval-dataset-drift:{dataset_id}:{version}",
                error_code="eval_dataset_content_drifted",
                error_summary=(
                    f"eval dataset {dataset_id!r} version {version} content has changed "
                    "since it was pinned; re-snapshot before releasing"
                ),
            )
        return ProviderOutcome(
            "applied",
            f"eval_dataset:{dataset_id}:{version}",
            provider_result={
                "version": version,
                "content_sha256": live_sha256,
                "example_count": len(examples),
            },
        )


__all__ = [
    "EVAL_DATASET_ADAPTER_VERSION",
    "EvalDatasetWorkspaceResourceAdapter",
    "ResolvedEvalDataset",
]
