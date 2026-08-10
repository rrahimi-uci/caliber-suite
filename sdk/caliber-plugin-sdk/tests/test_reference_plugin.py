"""The reference optimizer's behaviour, including when it declines.

The declining paths matter more than the happy one: every one of them is a case
where producing a candidate anyway would waste a human review.
"""

from __future__ import annotations

import pytest

from caliber_plugin_sdk import Diagnosis, OptimizationRequest, OptimizerUnavailable
from caliber_plugin_sdk.reference import RequirementAppender


def request_for(
    content: str = "You are a helpful assistant.",
    *,
    root_cause: str = "the prompt never tells the agent to call lookup_policy",
    confidence: float = 0.9,
    review_notes: str | None = None,
) -> OptimizationRequest:
    return OptimizationRequest(
        artifact_type="prompt",
        current_content=content,
        diagnosis=Diagnosis(root_cause=root_cause, confidence=confidence),
        review_notes=review_notes,
    )


def test_the_root_cause_becomes_an_explicit_requirement() -> None:
    result = RequirementAppender().optimize(request_for())
    assert "You are a helpful assistant." in result.content
    assert "MUST address" in result.content
    assert "lookup_policy" in result.content
    assert result.rationale


def test_cold_start_produces_the_requirement_as_the_whole_artifact() -> None:
    """No existing artifact to extend. A plugin that indexed into the content
    would raise here, on the first refinement a new agent ever runs."""
    result = RequirementAppender().optimize(request_for(""))
    assert result.content.startswith("You MUST address")


def test_reviewer_guidance_is_incorporated_rather_than_ignored() -> None:
    """Ignoring it means proposing the same shape of change that was rejected."""
    result = RequirementAppender().optimize(
        request_for(review_notes="Be specific about which policy version to look up.")
    )
    assert "policy version" in result.content
    assert "reviewer" in result.rationale.lower()


def test_a_low_confidence_diagnosis_is_declined() -> None:
    """Appending a hard requirement derived from a guess makes the artifact
    worse in a way the eval gate may not catch."""
    with pytest.raises(OptimizerUnavailable, match="too low"):
        RequirementAppender().optimize(request_for(confidence=0.3))


def test_an_empty_root_cause_is_declined() -> None:
    with pytest.raises(OptimizerUnavailable, match="no root cause"):
        RequirementAppender().optimize(request_for(root_cause="   "))


def test_an_already_applied_requirement_is_declined_rather_than_repeated() -> None:
    """A no-op candidate costs a human review, the scarcest thing in the loop."""
    first = RequirementAppender().optimize(request_for())
    with pytest.raises(OptimizerUnavailable, match="already present"):
        RequirementAppender().optimize(request_for(first.content))


def test_a_mechanical_optimizer_reports_zero_tokens_rather_than_unknown() -> None:
    """Zero is the true number; ``None`` would claim the cost is opaque."""
    result = RequirementAppender().optimize(request_for())
    assert result.total_tokens == 0
    assert result.cost_usd == 0.0
