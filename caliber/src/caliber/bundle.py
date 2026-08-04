"""Transactional and compensating bundle-promotion helper.

When a refinement job spans multiple agents/artifacts (its
``bundle_targets`` list has more than zero entries beyond the primary
agent), approving the job needs to land *all* artifact changes or
none. A partial state where artifact A has been rotated but artifact B
failed mid-flight leaves the fleet in an inconsistent place: agents
that share a prompt now disagree, downstream callers see a mix of old
and new behavior, and rolling back is non-obvious because each
artifact lives in a separate registry slot.

This module centralizes the best available failure handling so
:mod:`caliber.apply` can stay readable. Database-resident skill writes share
the caller transaction; external registry/provider effects use best-effort
compensation and are not SQL-atomic. The flow:

1. Build a :class:`BundleTarget` list from the job's ``bundle_targets``
   (or a single-element list synthesized from the job's own
   ``agent_id`` / ``artifact_type`` for non-bundle jobs — that way the
   approve path takes the same code path uniformly).
2. Promote each target in order, accumulating successful results.
3. If a promotion raises :class:`PromoterError`, let the caller transaction
   unwind DB-resident skill writes and best-effort roll back already-promoted
   external targets in reverse order before re-raising.

Rollback-on-failure is best-effort. If a rollback itself fails, we
log the inconsistency clearly (operator-actionable) but continue
trying the remaining rollbacks rather than failing fast — the
operator gets the full picture of what landed and what didn't.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from caliber.db.models import CaliberRefinementJob
from caliber.promoter import (
    Promoter,
    PromoterConflictError,
    PromoterError,
    PromotionRequest,
    PromotionResult,
    RollbackRequest,
)

logger = logging.getLogger("caliber.bundle")


@dataclass(frozen=True)
class BundleTarget:
    """One artifact promotion within a bundle.

    ``content`` is the candidate content to land at this target.
    Single-artifact jobs pass the same content as the approval's
    canonical candidate; multi-artifact bundles can pass per-target
    content when the optimizer produces it.
    """

    agent_id: str
    artifact_type: str
    content: str
    rationale: str


@dataclass(frozen=True)
class BundlePromotionError(Exception):
    """Raised when a bundle promotion partially fails after rollback.

    Subclass of ``Exception`` (not ``PromoterError``) so the caller's
    existing ``except PromoterError`` block can choose to catch this
    via the str-formatted message instead.

    Attributes
    ----------
    failing_target:
        The target that raised. Other targets either succeeded (and
        were rolled back) or were never attempted.
    underlying:
        The original :class:`PromoterError` from the failing call.
    succeeded_before_failure:
        Targets that promoted successfully before the failure. Each
        had a rollback attempted; failures are in
        ``rollback_failures``.
    rollback_failures:
        Targets where rollback also failed. Operator-actionable —
        these are the rows that are out of sync with CALIBER's view.
    """

    failing_target: BundleTarget
    underlying: PromoterError
    succeeded_before_failure: tuple[BundleTarget, ...]
    rollback_failures: tuple[BundleTarget, ...]

    def __post_init__(self) -> None:
        # Dataclasses with frozen=True + Exception don't auto-init the
        # message; we set it via ``super().__init__()`` so str(exc) is
        # informative.
        super().__init__(str(self))

    def __str__(self) -> str:
        msg = (
            f"bundle promotion failed at "
            f"{self.failing_target.agent_id}/{self.failing_target.artifact_type}: "
            f"{self.underlying}"
        )
        if self.rollback_failures:
            failed = ", ".join(f"{t.agent_id}/{t.artifact_type}" for t in self.rollback_failures)
            msg = f"{msg}; rollback also failed for [{failed}]"
        return msg


def _coerce_str(value: object, fallback: str) -> str:
    """Coerce a JSON value to a non-empty string, falling back when unsafe.

    The naive ``str(dict.get(key, fallback))`` pattern returns the
    literal string ``"None"`` when the key is *present but ``null``*
    (because ``dict.get`` only substitutes the fallback for *missing*
    keys, and ``str(None)`` is ``"None"``). That can land a promotion
    against ``agent_id="None"`` or ``artifact_type="None"`` — a
    silent corruption of the target identifier (deep-review
    Finding 6). This helper treats anything that isn't a non-empty
    string as "use the fallback."
    """
    if isinstance(value, str) and value:
        return value
    return fallback


def resolve_bundle_targets(
    job: CaliberRefinementJob,
    *,
    candidate_content: str,
    candidate_rationale: str,
) -> list[BundleTarget]:
    """Build the target list for the job.

    Single-artifact jobs (the common case — ``bundle_targets`` empty)
    return one entry derived from the job's own agent + artifact_type
    so the approve path can take the same code branch as a real
    bundle.

    Multi-artifact jobs return one entry per ``bundle_targets`` row.
    Each row's ``content`` defaults to the approval's main candidate
    content unless the row itself carries a ``content`` field — that
    lets a future per-target-candidate optimizer ship without
    changing this helper's contract.

    Bundle entries with ``null`` or non-string values for the
    ``agent_id`` / ``artifact_type`` / ``content`` / ``rationale``
    keys fall back to the job-level defaults rather than coercing to
    literal ``"None"`` strings.
    """
    primary_target_id = (
        job.skill_name
        if job.artifact_type == "skill" and isinstance(job.skill_name, str) and job.skill_name
        else job.agent_id
    )
    raw = job.bundle_targets or []
    if not raw:
        return [
            BundleTarget(
                agent_id=primary_target_id,
                artifact_type=job.artifact_type,
                content=candidate_content,
                rationale=candidate_rationale,
            )
        ]
    targets: list[BundleTarget] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        targets.append(
            BundleTarget(
                agent_id=_coerce_str(entry.get("agent_id"), primary_target_id),
                artifact_type=_coerce_str(entry.get("artifact_type"), job.artifact_type),
                content=_coerce_str(entry.get("content"), candidate_content),
                rationale=_coerce_str(entry.get("rationale"), candidate_rationale),
            )
        )
    if not targets:
        return [
            BundleTarget(
                agent_id=primary_target_id,
                artifact_type=job.artifact_type,
                content=candidate_content,
                rationale=candidate_rationale,
            )
        ]
    return targets


def promote_bundle(
    promoter: Promoter,
    targets: list[BundleTarget],
    *,
    approval_id: str,
    session: Any | None = None,
    actor: str = "",
) -> list[PromotionResult]:
    """Promote every target with transactional or compensating rollback.

    Returns the list of results in the same order as ``targets``.
    DB-resident skill writes use ``session`` and roll back with the caller's
    transaction. External targets are compensated best-effort when a later
    target fails; those provider effects cannot be made SQL-atomic.
    """
    succeeded: list[tuple[BundleTarget, PromotionResult]] = []
    for target in targets:
        try:
            result = promoter.promote(
                PromotionRequest(
                    agent_id=target.agent_id,
                    artifact_type=target.artifact_type,
                    new_content=target.content,
                    rationale=target.rationale,
                    approval_id=approval_id,
                    # DB-resident skills mutate this session directly. External
                    # prompt promoters use it only for the durable release intent
                    # and settlement around their alias effect.
                    session=session,
                    actor=actor,
                )
            )
        except PromoterError as exc:
            rollback_failures = _rollback_succeeded(
                promoter,
                succeeded,
                session=session,
                actor=actor,
            )
            failure = BundlePromotionError(
                failing_target=target,
                underlying=exc,
                succeeded_before_failure=tuple(t for t, _ in succeeded),
                rollback_failures=tuple(rollback_failures),
            )
            logger.error(
                "bundle promotion failed for approval=%s: %s",
                approval_id,
                failure,
            )
            error_type = (
                PromoterConflictError if isinstance(exc, PromoterConflictError) else PromoterError
            )
            raise error_type(str(failure)) from exc
        succeeded.append((target, result))

    return [result for _, result in succeeded]


def _rollback_succeeded(
    promoter: Promoter,
    succeeded: list[tuple[BundleTarget, PromotionResult]],
    *,
    session: Any | None,
    actor: str,
) -> list[BundleTarget]:
    """Roll back already-promoted targets in reverse order.

    Returns the targets whose rollback itself failed. The caller
    surfaces these to the operator — they're the rows that need
    manual reconciliation.
    """
    failures: list[BundleTarget] = []
    for target, result in reversed(succeeded):
        # SkillPromoter writes only into the caller-owned transaction. A later
        # failure unwinds that session, so invoking generic rollback here would
        # both be unnecessary and require a checkpoint that is not created yet.
        if target.artifact_type == "skill" and session is not None:
            continue
        details = getattr(result, "details", None) or {}
        version_after = _coerce_int(details.get("version") if isinstance(details, dict) else None)
        version_before = (
            version_after - 1 if version_after is not None and version_after > 1 else None
        )
        try:
            promoter.rollback(
                RollbackRequest(
                    agent_id=target.agent_id,
                    artifact_type=target.artifact_type,
                    version_before=version_before,
                    # No checkpoint row exists yet for this promotion
                    # (the bundle is mid-approve), so we pass a synthetic
                    # marker. The promoter implementations only use this
                    # for logging, not for state.
                    checkpoint_id=f"bundle-rollback:{target.agent_id}:{target.artifact_type}",
                    session=session,
                    actor=actor,
                )
            )
        except Exception:
            logger.exception(
                "rollback failed for already-promoted target %s/%s; manual intervention needed",
                target.agent_id,
                target.artifact_type,
            )
            failures.append(target)
    return failures


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
