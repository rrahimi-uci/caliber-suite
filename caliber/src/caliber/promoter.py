"""Artifact promotion — the final "deploy this candidate" step.

When a reviewer approves a refinement, CALIBER needs to make the new
artifact the active one. For prompts that's typically a register-prompt-version
+ alias-rotation against the MLflow Prompt Registry; for other artifact
types it's an artifact upload + ref bump.

Same dependency-injection pattern as :mod:`caliber.llm.provider`,
:mod:`caliber.eval.provider`, and :mod:`caliber.artifact_store`:

* :class:`Promoter` Protocol — the surface routes depend on.
* :class:`FakePromoter` — in-memory test double that records what would
  have been promoted.
* :class:`MLflowPromoter` — production wrapper around
  ``mlflow.register_prompt`` + alias rotation. Stubbed today; the real
  alias-rotation call lands in a follow-up milestone alongside the
  ``predict_fn`` registration for the eval runner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from caliber.release_operations import (
    ReleaseOperationConflictError,
    execute_prompt_alias_release,
    prepare_prompt_alias_release,
)

logger = logging.getLogger("caliber.promoter")


@dataclass(frozen=True)
class PromotionRequest:
    """Input to :meth:`Promoter.promote`.

    Carries everything the promoter needs to perform the deploy without
    re-querying CALIBER state — the approval row's denormalized snapshots
    are the source.
    """

    agent_id: str
    artifact_type: str
    new_content: str
    rationale: str
    approval_id: str
    # DB-resident promoters participate in the route's transaction. External
    # promoters ignore these fields.
    session: Any | None = field(default=None, repr=False, compare=False)
    actor: str = ""


@dataclass(frozen=True)
class PromotionResult:
    """Output of :meth:`Promoter.promote`.

    ``artifact_ref`` is the durable identifier of the just-promoted artifact
    (e.g. an MLflow prompt URI with the new version + alias). Stored on the
    audit log so an auditor can resolve a promotion to its exact artifact.
    """

    artifact_ref: str
    rotated_at: datetime
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RollbackRequest:
    """Input to :meth:`Promoter.rollback`.

    Carries the minimum identifying info: which agent, which artifact type,
    and which version to roll *back to*. The caller (the rollback endpoint)
    reads these from a stored :class:`caliber.db.models.CaliberRollbackCheckpoint`
    row so the rollback never re-derives state from MLflow tags.
    """

    agent_id: str
    artifact_type: str
    version_before: int | None
    checkpoint_id: str
    # DB-resident promoters participate in the route's transaction. External
    # promoters ignore these fields.
    session: Any | None = field(default=None, repr=False, compare=False)
    actor: str = ""


class PromoterError(Exception):
    """Raised when a promotion fails (auth, registry unavailable, etc.).

    The approval endpoint catches this and surfaces a 502 to the caller so
    they can retry or escalate. The job status is *not* mutated — the
    approval stays in ``pending`` for retry.
    """


class PromoterConflictError(PromoterError):
    """Raised when a concurrent DB promotion claims the same version."""


class Promoter(Protocol):
    """Promotion surface CALIBER depends on."""

    def promote(self, request: PromotionRequest) -> PromotionResult: ...

    def rollback(self, request: RollbackRequest) -> PromotionResult: ...


# ---------------------------------------------------------------------------
# Production promoter
# ---------------------------------------------------------------------------


class MLflowPromoter:
    """Production promoter against the MLflow Prompt Registry.

    Registers a new prompt version with the candidate content, then rotates
    the ``{agent_id}@{alias}`` alias to point at it. The previous version
    stays in the registry (MLflow keeps every registered version), so a
    rollback is just another :meth:`mlflow.set_prompt_alias` call against
    the prior version number.

    Multi-artifact atomic-bundle promotion is
    not implemented here — that goes through a dedicated bundle promoter
    in a future milestone. This class handles one prompt at a time.
    """

    def __init__(self, alias: str = "prod") -> None:
        self._alias = alias

    def promote(self, request: PromotionRequest) -> PromotionResult:
        # Lazy-import mlflow so the module stays importable in test
        # environments that mock the promoter. MLflow 3.13+ moved the
        # prompt-registry surface into the ``mlflow.genai`` namespace and
        # emits ``FutureWarning`` from the legacy module-level alias; we
        # prefer the new namespace and fall back to the old one only on
        # older builds.
        try:
            import mlflow  # noqa: PLC0415
        except ImportError as exc:
            raise PromoterError(
                "mlflow is not installed; install caliber with its "
                "default dependencies to use MLflowPromoter."
            ) from exc

        if request.artifact_type != "prompt":
            raise PromoterError(
                f"MLflowPromoter only supports artifact_type='prompt'; "
                f"got {request.artifact_type!r}"
            )

        register_prompt = _resolve_prompt_api(mlflow, "register_prompt")
        set_prompt_alias = _resolve_prompt_api(mlflow, "set_prompt_alias")

        prompt_name = request.agent_id

        # Capture the version currently live on the alias BEFORE we rotate it, so
        # the rollback checkpoint records the EXACT prior target. Deriving it as
        # ``version_after - 1`` is wrong whenever intermediate versions were
        # registered without rotating the alias.
        version_before = (
            _release_alias_version(mlflow, prompt_name, self._alias)
            if request.session is not None
            else _current_alias_version(mlflow, prompt_name, self._alias)
        )

        # 1. Register the new version under the agent's prompt name.
        try:
            version = register_prompt(
                name=prompt_name,
                template=request.new_content,
                commit_message=request.rationale or "promoted by CALIBER",
                tags={
                    "caliber.approval_id": request.approval_id,
                    "caliber.artifact_type": request.artifact_type,
                },
            )
        except Exception as exc:
            logger.exception("register_prompt failed for %s", prompt_name)
            raise PromoterError(
                f"failed to register new version of {prompt_name!r}: {exc}"
            ) from exc

        version_number = int(getattr(version, "version", 0))
        artifact_ref = getattr(version, "uri", f"prompts:/{prompt_name}/{version_number}")

        # 2. Rotate the alias to point at the new version. Apply supplies its
        # caller-owned session, allowing the same durable intent-first state
        # machine used by direct/existing-version prompt releases. Keeping the
        # session optional preserves this low-level adapter for isolated registry
        # integration tests, but no in-repository release route omits it.
        try:
            if request.session is not None:
                operation = prepare_prompt_alias_release(
                    request.session,
                    name=prompt_name,
                    alias=self._alias,
                    version_before=version_before,
                    version_after=version_number,
                    actor=request.actor or "system:promoter",
                    effective_scopes=("operator",),
                    evidence={
                        "gate_state": "pass",
                        "source": "candidate_ready",
                        "approval_id": request.approval_id,
                    },
                    approval_id=request.approval_id,
                )

                def mutate_alias(*, name: str, alias: str, version: int) -> dict[str, object]:
                    set_prompt_alias(name=name, alias=alias, version=version)
                    return {"name": name, "alias": alias, "version": version}

                execute_prompt_alias_release(
                    request.session,
                    operation,
                    mutate_alias=mutate_alias,
                )
            else:
                set_prompt_alias(name=prompt_name, alias=self._alias, version=version_number)
        except ReleaseOperationConflictError as exc:
            raise PromoterConflictError(str(exc)) from exc
        except Exception as exc:
            # Once a provider call begins its outcome can be indeterminate. Do
            # not delete the registered target here: the alias may already point
            # at it. The durable operation (when a session was supplied) is the
            # authority for reconciliation.
            logger.exception("set_prompt_alias failed for %s", prompt_name)
            raise PromoterError(
                f"registered version {version_number} of {prompt_name!r} but "
                f"alias rotation to {self._alias!r} needs reconciliation: {exc}"
            ) from exc

        return PromotionResult(
            artifact_ref=str(artifact_ref),
            rotated_at=datetime.now(timezone.utc),
            details={
                "name": prompt_name,
                "version": version_number,
                "version_before": version_before,
                "alias": self._alias,
            },
        )

    def rollback(self, request: RollbackRequest) -> PromotionResult:
        """Rotate the alias back to ``request.version_before``.

        Cold-start checkpoints (``version_before is None``) cannot be
        rolled back — there is no prior version to point at. We surface
        that as a :class:`PromoterError` so the rollback endpoint can
        return 409 to the caller.
        """
        if request.artifact_type != "prompt":
            raise PromoterError(
                f"MLflowPromoter only supports artifact_type='prompt'; "
                f"got {request.artifact_type!r}"
            )
        if request.version_before is None:
            raise PromoterError(
                f"checkpoint {request.checkpoint_id!r} is a cold-start record "
                "with no prior version; nothing to roll back to."
            )

        try:
            import mlflow  # noqa: PLC0415
        except ImportError as exc:
            raise PromoterError(
                "mlflow is not installed; install caliber with its "
                "default dependencies to use MLflowPromoter."
            ) from exc

        set_prompt_alias = _resolve_prompt_api(mlflow, "set_prompt_alias")
        version_live = (
            _release_alias_version(mlflow, request.agent_id, self._alias)
            if request.session is not None
            else _current_alias_version(mlflow, request.agent_id, self._alias)
        )
        try:
            if request.session is not None:
                operation = prepare_prompt_alias_release(
                    request.session,
                    name=request.agent_id,
                    alias=self._alias,
                    version_before=version_live,
                    version_after=request.version_before,
                    actor=request.actor or "system:promoter",
                    operation_type="rollback",
                    effective_scopes=("operator",),
                    evidence={
                        "source": "rollback_checkpoint",
                        "checkpoint_id": request.checkpoint_id,
                    },
                )

                def mutate_alias(*, name: str, alias: str, version: int) -> dict[str, object]:
                    set_prompt_alias(name=name, alias=alias, version=version)
                    return {"name": name, "alias": alias, "version": version}

                execute_prompt_alias_release(
                    request.session,
                    operation,
                    mutate_alias=mutate_alias,
                )
            else:
                set_prompt_alias(
                    name=request.agent_id,
                    alias=self._alias,
                    version=request.version_before,
                )
        except ReleaseOperationConflictError as exc:
            raise PromoterConflictError(str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "rollback set_prompt_alias failed for %s -> v%s",
                request.agent_id,
                request.version_before,
            )
            raise PromoterError(
                f"rollback of {request.agent_id!r} to v{request.version_before} "
                f"needs reconciliation: {exc}"
            ) from exc

        return PromotionResult(
            artifact_ref=f"prompts:/{request.agent_id}/{request.version_before}",
            rotated_at=datetime.now(timezone.utc),
            details={
                "name": request.agent_id,
                "version": request.version_before,
                "alias": self._alias,
                "rollback": True,
                "checkpoint_id": request.checkpoint_id,
                "version_before": version_live,
                "operation_id": (operation.operation_id if request.session is not None else None),
            },
        )


def _resolve_prompt_api(mlflow_mod: object, name: str) -> Any:
    """Return ``mlflow.genai.<name>`` when available, else ``mlflow.<name>``.

    MLflow 3.13+ moved the prompt-registry surface into ``mlflow.genai``;
    older builds expose it at the module level. We try the new location
    first so callers don't trip the ``FutureWarning`` from the legacy
    alias on a strict-warning suite.
    """
    genai = getattr(mlflow_mod, "genai", None)
    if genai is not None:
        fn = getattr(genai, name, None)
        if fn is not None:
            return fn
    fn = getattr(mlflow_mod, name, None)
    if fn is None:
        raise PromoterError(
            f"mlflow does not expose {name!r} in either ``mlflow.genai`` or the "
            "top-level namespace; upgrade mlflow to 3.12 or later."
        )
    return fn


def _current_alias_version(mlflow_mod: object, name: str, alias: str) -> int | None:
    """Version currently live on ``name@alias``, or ``None`` on cold start.

    Best-effort: returns ``None`` when the load API is unavailable, the alias
    doesn't resolve yet, or the version can't be coerced to an int — callers
    fall back to the legacy ``version_after - 1`` derivation in that case.
    """
    try:
        load_prompt = _resolve_prompt_api(mlflow_mod, "load_prompt")
    except PromoterError:
        return None
    try:
        prompt = load_prompt(f"prompts:/{name}@{alias}")
    except Exception:
        return None
    if prompt is None:
        return None
    raw = getattr(prompt, "version", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _release_alias_version(mlflow_mod: object, name: str, alias: str) -> int | None:
    """Resolve the exact outgoing target or refuse to perform a release."""
    load_prompt = _resolve_prompt_api(mlflow_mod, "load_prompt")
    try:
        prompt = load_prompt(f"prompts:/{name}@{alias}", allow_missing=True)
    except Exception as exc:
        raise PromoterError(f"failed to resolve current alias {name!r}@{alias!r}: {exc}") from exc
    if prompt is None:
        return None
    raw = getattr(prompt, "version", None)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise PromoterError(f"current alias {name!r}@{alias!r} has no concrete version") from exc


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakePromoter:
    """In-memory promoter for tests and demos.

    Records every :class:`PromotionRequest` and returns a deterministic
    :class:`PromotionResult`. Tests can override the return value or wire
    a callable that simulates a transient failure.
    """

    result: PromotionResult | None = None
    fail_with: PromoterError | None = None
    calls: list[PromotionRequest] = field(default_factory=list)
    rollback_calls: list[RollbackRequest] = field(default_factory=list)
    rollback_fail_with: PromoterError | None = None

    def promote(self, request: PromotionRequest) -> PromotionResult:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        if self.result is not None:
            return self.result
        return PromotionResult(
            artifact_ref=f"prompt://{request.agent_id}/v-fake",
            rotated_at=datetime.now(timezone.utc),
            details={"alias": "prod", "approval_id": request.approval_id},
        )

    def rollback(self, request: RollbackRequest) -> PromotionResult:
        self.rollback_calls.append(request)
        if self.rollback_fail_with is not None:
            raise self.rollback_fail_with
        if request.version_before is None:
            raise PromoterError(
                f"checkpoint {request.checkpoint_id!r} has no prior version; nothing to roll back."
            )
        return PromotionResult(
            artifact_ref=f"prompt://{request.agent_id}/v{request.version_before}",
            rotated_at=datetime.now(timezone.utc),
            details={
                "alias": "prod",
                "version": request.version_before,
                "rollback": True,
                "checkpoint_id": request.checkpoint_id,
            },
        )


# ---------------------------------------------------------------------------
# Skill promotion (DB-side, not MLflow)
# ---------------------------------------------------------------------------


class SkillPromoter:
    """Promotes skill artifacts by updating their content in the CALIBER DB.

    Skills live in ``caliber_skills``, not in the MLflow Prompt Registry.
    Promotion means updating the row's ``content``, bumping ``version``,
    recording an immutable ``CaliberSkillVersion`` snapshot, and tagging
    ``skill_metadata`` with the approval lineage.

    Both promotion and rollback require the caller's SQLAlchemy session so the
    live row, immutable history, checkpoint, and audit record commit or roll
    back together. Rollback restores prior content as a *new* monotonically
    increasing version.
    """

    def promote(self, request: PromotionRequest) -> PromotionResult:
        if request.artifact_type != "skill":
            raise PromoterError(
                f"SkillPromoter only supports artifact_type='skill'; got {request.artifact_type!r}"
            )

        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        from caliber.db.models import CaliberSkill  # noqa: PLC0415
        from caliber.skill_versions import (  # noqa: PLC0415
            ensure_skill_version_snapshot,
            record_skill_version,
        )

        session = request.session
        if session is None:
            raise PromoterError("SkillPromoter requires a caller-owned session")

        # ``agent_id`` is overloaded: for skill jobs, routes store the
        # skill name in the approval request's ``agent_id`` field (or
        # a dedicated ``skill_name`` field if we extend the approval
        # model later). For now, look it up from the refinement job
        # or fall back to treating ``agent_id`` as the skill name.
        skill_name = request.agent_id

        try:
            skill = (
                session.execute(
                    select(CaliberSkill)
                    .where(
                        CaliberSkill.name == skill_name,
                        CaliberSkill.status == "active",
                    )
                    .with_for_update()
                )
                .scalars()
                .first()
            )
            if skill is None:
                raise PromoterError(f"skill {skill_name!r} not found or archived; cannot promote.")
            old_version = skill.version
            old_content = skill.content
            old_summary = skill.summary or ""
            created_by = request.actor or f"approval:{request.approval_id}"
            history_head = ensure_skill_version_snapshot(
                session,
                skill.skill_id,
                old_version,
                old_content,
                old_summary,
                created_by=created_by,
            )
            skill.content = request.new_content
            skill.version = history_head + 1
            meta = dict(skill.skill_metadata or {})
            meta["last_approval_id"] = request.approval_id
            meta["last_promoted_at"] = datetime.now(timezone.utc).isoformat()
            skill.skill_metadata = meta
            record_skill_version(session, skill, created_by=created_by)
            session.flush()

            return PromotionResult(
                artifact_ref=f"skill://{skill_name}/v{skill.version}",
                rotated_at=datetime.now(timezone.utc),
                details={
                    "skill_name": skill_name,
                    "old_version": old_version,
                    "new_version": skill.version,
                    "version": skill.version,
                    "content_before": old_content,
                    "summary_before": old_summary,
                    "version_before": history_head,
                },
            )
        except PromoterError:
            raise
        except IntegrityError as exc:
            raise PromoterConflictError(
                f"skill {skill_name!r} was modified concurrently; reload and retry."
            ) from exc
        except Exception as exc:
            logger.exception("skill promotion failed for %s", skill_name)
            raise PromoterError(f"failed to promote skill {skill_name!r}: {exc}") from exc

    def rollback(  # noqa: PLR0915 - validation and transactional restore stay together
        self, request: RollbackRequest
    ) -> PromotionResult:
        if request.artifact_type != "skill":
            raise PromoterError(
                f"SkillPromoter only supports artifact_type='skill'; got {request.artifact_type!r}"
            )

        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        from caliber.db.models import (  # noqa: PLC0415
            CaliberRollbackCheckpoint,
            CaliberSkill,
            CaliberSkillVersion,
        )
        from caliber.skill_versions import (  # noqa: PLC0415
            ensure_skill_version_snapshot,
            record_skill_version,
        )

        session = request.session
        if session is None:
            raise PromoterError("SkillPromoter requires a caller-owned session")

        try:
            checkpoint = session.get(CaliberRollbackCheckpoint, request.checkpoint_id)
            if checkpoint is None:
                raise PromoterError(
                    f"rollback checkpoint {request.checkpoint_id!r} not found; "
                    "skill rollback restores the snapshot recorded at promotion."
                )
            payload = checkpoint.snapshot_payload or {}
            content_before = payload.get("content_before")
            if not isinstance(content_before, str):
                raise PromoterError(
                    f"checkpoint {request.checkpoint_id!r} has no skill content snapshot; "
                    "cannot roll back (was it created before skill snapshots were recorded?)."
                )

            skill_name = checkpoint.artifact_name or request.agent_id
            skill = (
                session.execute(
                    select(CaliberSkill).where(CaliberSkill.name == skill_name).with_for_update()
                )
                .scalars()
                .first()
            )
            if skill is None:
                raise PromoterError(f"skill {skill_name!r} not found; cannot roll back.")

            restored_from_version = request.version_before
            if restored_from_version is None:
                snap_version = payload.get("version_before")
                restored_from_version = snap_version if isinstance(snap_version, int) else None
            summary_before = payload.get("summary_before")
            if not isinstance(summary_before, str) and isinstance(restored_from_version, int):
                source = (
                    session.execute(
                        select(CaliberSkillVersion).where(
                            CaliberSkillVersion.skill_id == skill.skill_id,
                            CaliberSkillVersion.version_number == restored_from_version,
                        )
                    )
                    .scalars()
                    .first()
                )
                if source is not None:
                    summary_before = source.summary
            if not isinstance(summary_before, str):
                summary_before = skill.summary or ""

            created_by = request.actor or f"rollback:{request.checkpoint_id}"
            history_head = ensure_skill_version_snapshot(
                session,
                skill.skill_id,
                skill.version,
                skill.content,
                skill.summary or "",
                created_by=created_by,
            )
            previous_live_version = skill.version
            skill.content = content_before
            skill.summary = summary_before
            skill.version = history_head + 1
            meta = dict(skill.skill_metadata or {})
            meta["last_rollback_checkpoint_id"] = request.checkpoint_id
            meta["last_rolled_back_at"] = datetime.now(timezone.utc).isoformat()
            skill.skill_metadata = meta
            record_skill_version(session, skill, created_by=created_by)
            session.flush()

            return PromotionResult(
                artifact_ref=f"skill://{skill_name}/v{skill.version}",
                rotated_at=datetime.now(timezone.utc),
                details={
                    "skill_name": skill_name,
                    "restored_from_version": restored_from_version,
                    "previous_live_version": previous_live_version,
                    "new_version": skill.version,
                    "version": skill.version,
                    "checkpoint_id": request.checkpoint_id,
                    "rolled_back": True,
                },
            )
        except PromoterError:
            raise
        except IntegrityError as exc:
            raise PromoterConflictError(
                f"skill {request.agent_id!r} was modified concurrently; reload and retry."
            ) from exc
        except Exception as exc:
            logger.exception("skill rollback failed for checkpoint %s", request.checkpoint_id)
            raise PromoterError(f"failed to roll back skill: {exc}") from exc


# ---------------------------------------------------------------------------
# Composite promoter
# ---------------------------------------------------------------------------


class CompositePromoter:
    """Delegates to the right promoter based on ``artifact_type``.

    Routes ``"skill"`` requests to :class:`SkillPromoter` and everything
    else to the underlying ``default`` promoter (MLflow or Fake).
    """

    _OWN_ATTRS = frozenset({"_default", "_skill"})
    # Set via object.__setattr__ in __init__ (custom __setattr__ delegates);
    # annotate so mypy resolves delegated calls instead of falling through
    # __getattr__ to ``object``.
    _default: Promoter
    _skill: SkillPromoter

    def __init__(self, default: Promoter, skill: SkillPromoter | None = None) -> None:
        object.__setattr__(self, "_default", default)
        object.__setattr__(self, "_skill", skill or SkillPromoter())

    def __getattr__(self, name: str) -> object:
        """Delegate attribute access to the underlying default promoter.

        This allows tests to access FakePromoter-specific attributes
        (``calls``, ``fail_with``, etc.) through the composite wrapper.
        """
        return getattr(self._default, name)

    def __setattr__(self, name: str, value: object) -> None:
        """Delegate attribute writes to the underlying default promoter."""
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._default, name, value)

    def promote(self, request: PromotionRequest) -> PromotionResult:
        if request.artifact_type == "skill":
            return self._skill.promote(request)
        return self._default.promote(request)

    def rollback(self, request: RollbackRequest) -> PromotionResult:
        if request.artifact_type == "skill":
            return self._skill.rollback(request)
        return self._default.rollback(request)


def build_promoter(provider: str) -> Promoter:
    """Factory keyed off ``CaliberConfig.promoter_provider``.

    Always wraps the base promoter in a :class:`CompositePromoter` so
    skill promotion works regardless of the backend provider.
    """
    norm = provider.lower()
    if norm == "fake":
        base: Promoter = FakePromoter()
    elif norm == "mlflow":
        base = MLflowPromoter()
    else:
        raise PromoterError(f"unknown promoter_provider {provider!r}")
    # SkillPromoter deliberately uses the caller-owned request session instead
    # of opening an inner transaction.
    return CompositePromoter(default=base, skill=SkillPromoter())
