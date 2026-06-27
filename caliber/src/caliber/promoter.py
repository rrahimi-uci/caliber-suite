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


class PromoterError(Exception):
    """Raised when a promotion fails (auth, registry unavailable, etc.).

    The approval endpoint catches this and surfaces a 502 to the caller so
    they can retry or escalate. The job status is *not* mutated — the
    approval stays in ``pending`` for retry.
    """


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

        # 2. Rotate the alias to point at the new version.
        try:
            set_prompt_alias(name=prompt_name, alias=self._alias, version=version_number)
        except Exception as exc:
            # The version is registered but the alias didn't rotate.
            # Best-effort cleanup: delete the orphaned version so the
            # retry path (operator clicks approve again, which calls
            # ``promoter.promote`` again) doesn't keep stacking
            # duplicate versions tagged with the same ``approval_id``.
            # If the delete itself fails we log it; the operator gets
            # the failing version_number in the surfaced PromoterError
            # so they can manually reconcile.
            logger.exception("set_prompt_alias failed for %s", prompt_name)
            delete_failed = _try_delete_prompt_version(
                mlflow, name=prompt_name, version=version_number
            )
            extra = (
                ""
                if not delete_failed
                else f" Cleanup of v{version_number} also failed: {delete_failed}."
            )
            raise PromoterError(
                f"registered version {version_number} of {prompt_name!r} but "
                f"alias rotation to {self._alias!r} failed: {exc}.{extra}"
            ) from exc

        return PromotionResult(
            artifact_ref=str(artifact_ref),
            rotated_at=datetime.now(timezone.utc),
            details={
                "name": prompt_name,
                "version": version_number,
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
        try:
            set_prompt_alias(
                name=request.agent_id,
                alias=self._alias,
                version=request.version_before,
            )
        except Exception as exc:
            logger.exception(
                "rollback set_prompt_alias failed for %s -> v%s",
                request.agent_id,
                request.version_before,
            )
            raise PromoterError(
                f"failed to roll {request.agent_id!r} back to v{request.version_before}: {exc}"
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
            },
        )


def _try_delete_prompt_version(
    mlflow_mod: object,
    *,
    name: str,
    version: int,
) -> str | None:
    """Best-effort delete of an orphaned prompt version.

    Used by :meth:`MLflowPromoter.promote` when alias rotation fails
    after a successful ``register_prompt`` — without the cleanup, a
    retried approval would stack duplicate versions tagged with the
    same ``approval_id``. Returns ``None`` on success or when the
    delete API isn't exposed; returns a stringified error on failure
    so the caller can surface it through the operator-facing
    :class:`PromoterError`.
    """
    try:
        delete_fn = _resolve_prompt_api(mlflow_mod, "delete_prompt_version")
    except PromoterError:
        # The installed mlflow build doesn't expose a delete API. Log
        # and move on — the operator must manually reconcile in this
        # rare case.
        logger.warning(
            "mlflow does not expose delete_prompt_version; orphaned v%d of %r left in place",
            version,
            name,
        )
        return None
    try:
        delete_fn(name=name, version=version)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "cleanup delete_prompt_version failed for %s v%d",
            name,
            version,
        )
        return str(exc)


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
    and recording a ``skill_metadata`` tag with the approval lineage.

    Rollback restores the previous content stored in the
    ``CaliberRollbackCheckpoint.content_before`` field and decrements
    version.
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    def promote(self, request: PromotionRequest) -> PromotionResult:
        if request.artifact_type != "skill":
            raise PromoterError(
                f"SkillPromoter only supports artifact_type='skill'; got {request.artifact_type!r}"
            )

        from caliber.db.models import CaliberSkill  # noqa: PLC0415

        if self._session_factory is None:
            # The app always wires a session factory through ``build_promoter``;
            # a None factory means a misconfigured/direct construction. Fail
            # clearly rather than touching an undeclared default database.
            raise PromoterError("SkillPromoter requires a session_factory")
        session_factory = self._session_factory

        # ``agent_id`` is overloaded: for skill jobs, routes store the
        # skill name in the approval request's ``agent_id`` field (or
        # a dedicated ``skill_name`` field if we extend the approval
        # model later). For now, look it up from the refinement job
        # or fall back to treating ``agent_id`` as the skill name.
        skill_name = request.agent_id

        try:
            with session_factory() as session:
                skill = (
                    session.query(CaliberSkill)
                    .filter(
                        CaliberSkill.name == skill_name,
                        CaliberSkill.status == "active",
                    )
                    .first()
                )
                if skill is None:
                    raise PromoterError(
                        f"skill {skill_name!r} not found or archived; cannot promote."
                    )
                old_version = skill.version
                old_content = skill.content
                skill.content = request.new_content
                skill.version = old_version + 1
                # Tag the skill's metadata with promotion lineage.
                meta = dict(skill.skill_metadata or {})
                meta["last_approval_id"] = request.approval_id
                meta["last_promoted_at"] = datetime.now(timezone.utc).isoformat()
                skill.skill_metadata = meta
                session.commit()

                return PromotionResult(
                    artifact_ref=f"skill://{skill_name}/v{skill.version}",
                    rotated_at=datetime.now(timezone.utc),
                    details={
                        "skill_name": skill_name,
                        "old_version": old_version,
                        "new_version": skill.version,
                        # ``version`` mirrors new_version so the generic checkpoint
                        # builder (which reads details["version"]) records the right
                        # version_after for skill promotions too.
                        "version": skill.version,
                        # Captured so rollback can restore the prior state — skills
                        # have no alias indirection, the active skill *is* the row.
                        "content_before": old_content,
                        "version_before": old_version,
                    },
                )
        except PromoterError:
            raise
        except Exception as exc:
            logger.exception("skill promotion failed for %s", skill_name)
            raise PromoterError(f"failed to promote skill {skill_name!r}: {exc}") from exc

    def rollback(self, request: RollbackRequest) -> PromotionResult:
        if request.artifact_type != "skill":
            raise PromoterError(
                f"SkillPromoter only supports artifact_type='skill'; got {request.artifact_type!r}"
            )

        from caliber.db.models import CaliberRollbackCheckpoint, CaliberSkill  # noqa: PLC0415

        if self._session_factory is None:
            raise PromoterError("SkillPromoter requires a session_factory")

        try:
            with self._session_factory() as session:
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
                skill = session.query(CaliberSkill).filter(CaliberSkill.name == skill_name).first()
                if skill is None:
                    raise PromoterError(f"skill {skill_name!r} not found; cannot roll back.")

                # Restore the pre-promotion content + version. ``version_before``
                # on the request (read off the checkpoint by the route) wins;
                # fall back to the snapshot copy.
                restored_version = request.version_before
                if restored_version is None:
                    snap_version = payload.get("version_before")
                    restored_version = snap_version if isinstance(snap_version, int) else None

                skill.content = content_before
                if isinstance(restored_version, int):
                    skill.version = restored_version
                meta = dict(skill.skill_metadata or {})
                meta["last_rollback_checkpoint_id"] = request.checkpoint_id
                meta["last_rolled_back_at"] = datetime.now(timezone.utc).isoformat()
                skill.skill_metadata = meta
                session.commit()

                return PromotionResult(
                    artifact_ref=f"skill://{skill_name}/v{skill.version}",
                    rotated_at=datetime.now(timezone.utc),
                    details={
                        "skill_name": skill_name,
                        "restored_version": skill.version,
                        "checkpoint_id": request.checkpoint_id,
                        "rolled_back": True,
                    },
                )
        except PromoterError:
            raise
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


def build_promoter(provider: str, session_factory: Any | None = None) -> Promoter:
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
    return CompositePromoter(default=base, skill=SkillPromoter(session_factory=session_factory))
