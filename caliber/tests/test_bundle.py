"""Tests for the atomic bundle-promotion helper.

Covers:

1. ``resolve_bundle_targets`` — synthesizes a single target from the
   job when ``bundle_targets`` is empty; one target per row otherwise;
   tolerates malformed rows.
2. ``promote_bundle`` — promotes all targets atomically; on failure,
   rolls back the already-promoted ones in reverse order.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from caliber.bundle import (
    BundleTarget,
    promote_bundle,
    resolve_bundle_targets,
)
from caliber.db.models import CaliberRefinementJob
from caliber.promoter import (
    PromoterConflictError,
    PromoterError,
    PromotionRequest,
    PromotionResult,
    RollbackRequest,
)


def _job(*, bundle_targets: list[dict[str, object]] | None = None) -> CaliberRefinementJob:
    return CaliberRefinementJob(
        job_id="RFN-test",
        agent_id="agent-primary",
        primary_item_id="FB-1",
        artifact_type="prompt",
        status="awaiting_approval",
        current_stage="approval",
        bundle_targets=bundle_targets if bundle_targets is not None else [],
    )


# ---------------------------------------------------------------------------
# resolve_bundle_targets
# ---------------------------------------------------------------------------


def test_resolve_synthesizes_single_target_when_empty() -> None:
    targets = resolve_bundle_targets(
        _job(),
        candidate_content="new prompt body",
        candidate_rationale="why",
    )
    assert len(targets) == 1
    t = targets[0]
    assert t.agent_id == "agent-primary"
    assert t.artifact_type == "prompt"
    assert t.content == "new prompt body"


def test_resolve_skill_uses_skill_name_not_agent_id() -> None:
    job = _job()
    job.artifact_type = "skill"
    job.skill_name = "tool-use"
    targets = resolve_bundle_targets(
        job,
        candidate_content="new skill body",
        candidate_rationale="why",
    )
    assert [(target.agent_id, target.artifact_type) for target in targets] == [
        ("tool-use", "skill")
    ]


def test_resolve_emits_one_target_per_row() -> None:
    targets = resolve_bundle_targets(
        _job(
            bundle_targets=[
                {"agent_id": "a-1", "artifact_type": "prompt"},
                {"agent_id": "a-2", "artifact_type": "tool_description"},
            ]
        ),
        candidate_content="shared content",
        candidate_rationale="r",
    )
    assert [(t.agent_id, t.artifact_type) for t in targets] == [
        ("a-1", "prompt"),
        ("a-2", "tool_description"),
    ]
    # When a bundle row doesn't carry its own content, it inherits
    # the approval's primary candidate.
    assert all(t.content == "shared content" for t in targets)


def test_resolve_per_target_content_overrides() -> None:
    """When the optimizer writes per-target content, it flows through."""
    targets = resolve_bundle_targets(
        _job(
            bundle_targets=[{"agent_id": "a-1", "artifact_type": "prompt", "content": "specific"}]
        ),
        candidate_content="default-fallback",
        candidate_rationale="r",
    )
    assert targets[0].content == "specific"


def test_resolve_tolerates_malformed_rows() -> None:
    """Non-dict rows are dropped; an all-malformed bundle still
    synthesizes a single primary-target fallback so the approve path
    doesn't crash."""
    targets = resolve_bundle_targets(
        _job(bundle_targets=[None, "not a dict", 123]),  # type: ignore[list-item]
        candidate_content="c",
        candidate_rationale="r",
    )
    assert len(targets) == 1
    assert targets[0].agent_id == "agent-primary"


def test_resolve_falls_back_for_null_field_values() -> None:
    """``{"agent_id": None, ...}`` must fall back to the job's
    primary agent_id — *not* collapse to the literal string
    ``"None"`` (deep-review Finding 6).
    """
    targets = resolve_bundle_targets(
        _job(
            bundle_targets=[
                {"agent_id": None, "artifact_type": "prompt"},
                {"agent_id": "agent-real", "artifact_type": None},
                {"agent_id": "agent-real-2", "artifact_type": "prompt", "content": None},
            ]
        ),
        candidate_content="primary-content",
        candidate_rationale="primary-rationale",
    )
    assert len(targets) == 3
    # First row: null agent_id → job's primary; artifact_type preserved.
    assert targets[0].agent_id == "agent-primary"
    assert targets[0].artifact_type == "prompt"
    # Second row: null artifact_type → job's primary.
    assert targets[1].agent_id == "agent-real"
    assert targets[1].artifact_type == "prompt"
    # Third row: null content → falls back to candidate_content.
    assert targets[2].content == "primary-content"


def test_resolve_falls_back_for_non_string_values() -> None:
    """Non-string types (numbers, lists, dicts) are unsafe target
    identifiers and fall back the same way as null."""
    targets = resolve_bundle_targets(
        _job(
            bundle_targets=[
                {"agent_id": 42, "artifact_type": ["prompt"]},
            ]
        ),
        candidate_content="c",
        candidate_rationale="r",
    )
    assert len(targets) == 1
    assert targets[0].agent_id == "agent-primary"
    assert targets[0].artifact_type == "prompt"


def test_resolve_falls_back_for_empty_string_values() -> None:
    """Empty strings are treated as "missing" — a deployment that
    accidentally writes ``""`` shouldn't ship a target whose id is
    the empty string."""
    targets = resolve_bundle_targets(
        _job(bundle_targets=[{"agent_id": "", "artifact_type": ""}]),
        candidate_content="c",
        candidate_rationale="r",
    )
    assert targets[0].agent_id == "agent-primary"
    assert targets[0].artifact_type == "prompt"


# ---------------------------------------------------------------------------
# promote_bundle
# ---------------------------------------------------------------------------


class _RecordingPromoter:
    """Promoter test double with programmable failures + recording."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.promote_calls: list[PromotionRequest] = []
        self.rollback_calls: list[RollbackRequest] = []
        self._counter = 0

    def promote(self, request: PromotionRequest) -> PromotionResult:
        self.promote_calls.append(request)
        if request.agent_id in self.fail_on:
            raise PromoterError(f"simulated failure for {request.agent_id}")
        self._counter += 1
        return PromotionResult(
            artifact_ref=f"prompts:/{request.agent_id}/v{self._counter}",
            rotated_at=datetime.now(timezone.utc),
            details={"version": self._counter + 1},
        )

    def rollback(self, request: RollbackRequest) -> PromotionResult:
        self.rollback_calls.append(request)
        return PromotionResult(
            artifact_ref=f"prompts:/{request.agent_id}/v{request.version_before}",
            rotated_at=datetime.now(timezone.utc),
            details={"rolled_back": True},
        )


def test_promote_bundle_all_succeed() -> None:
    promoter = _RecordingPromoter()
    targets = [
        BundleTarget("a-1", "prompt", "c1", "r1"),
        BundleTarget("a-2", "prompt", "c2", "r2"),
    ]
    results = promote_bundle(promoter, targets, approval_id="AP-1")
    assert len(results) == 2
    assert [r.artifact_ref for r in results] == [
        "prompts:/a-1/v1",
        "prompts:/a-2/v2",
    ]
    assert promoter.rollback_calls == []


def test_promote_bundle_rolls_back_on_mid_failure() -> None:
    """Two succeed, the third fails — both successful ones get rolled
    back in reverse order, then PromoterError propagates."""
    promoter = _RecordingPromoter(fail_on={"a-3"})
    targets = [
        BundleTarget("a-1", "prompt", "c1", "r1"),
        BundleTarget("a-2", "prompt", "c2", "r2"),
        BundleTarget("a-3", "prompt", "c3", "r3"),
    ]
    with pytest.raises(PromoterError) as excinfo:
        promote_bundle(promoter, targets, approval_id="AP-1")
    assert "a-3" in str(excinfo.value)
    # Reverse order rollback.
    assert [r.agent_id for r in promoter.rollback_calls] == ["a-2", "a-1"]


def test_promote_bundle_first_target_failure_no_rollback() -> None:
    """If the *first* target fails there's nothing to roll back —
    just propagate the error."""
    promoter = _RecordingPromoter(fail_on={"a-1"})
    targets = [
        BundleTarget("a-1", "prompt", "c1", "r1"),
        BundleTarget("a-2", "prompt", "c2", "r2"),
    ]
    with pytest.raises(PromoterError):
        promote_bundle(promoter, targets, approval_id="AP-1")
    assert promoter.rollback_calls == []
    # Second target was never attempted.
    assert [r.agent_id for r in promoter.promote_calls] == ["a-1"]


def test_promote_bundle_preserves_retryable_conflict_type() -> None:
    class _ConflictPromoter(_RecordingPromoter):
        def promote(self, request: PromotionRequest) -> PromotionResult:
            raise PromoterConflictError("version already claimed")

    with pytest.raises(PromoterConflictError, match="version already claimed"):
        promote_bundle(
            _ConflictPromoter(),
            [BundleTarget("tool-use", "skill", "c", "r")],
            approval_id="AP-1",
            session=object(),
        )


def test_promote_bundle_continues_rollback_when_one_fails() -> None:
    """A rollback failure on one target doesn't stop the rest from
    being attempted — the caller still gets a single PromoterError
    that names the original failure plus the rollback failures."""

    class _PartialRollbackPromoter(_RecordingPromoter):
        def rollback(self, request: RollbackRequest) -> PromotionResult:
            self.rollback_calls.append(request)
            if request.agent_id == "a-2":
                raise RuntimeError("rollback of a-2 also broken")
            return PromotionResult(
                artifact_ref="prompts:/x/v1",
                rotated_at=datetime.now(timezone.utc),
                details={},
            )

    promoter = _PartialRollbackPromoter(fail_on={"a-3"})
    targets = [
        BundleTarget("a-1", "prompt", "c1", "r1"),
        BundleTarget("a-2", "prompt", "c2", "r2"),
        BundleTarget("a-3", "prompt", "c3", "r3"),
    ]
    with pytest.raises(PromoterError) as excinfo:
        promote_bundle(promoter, targets, approval_id="AP-1")
    # Both rollbacks attempted even though a-2's failed.
    assert [r.agent_id for r in promoter.rollback_calls] == ["a-2", "a-1"]
    # The exception message surfaces the rollback failure for
    # operator-actionable visibility.
    assert "a-2" in str(excinfo.value)
