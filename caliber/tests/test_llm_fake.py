"""Tests for the deterministic LLM provider used by tests and the default boot.

The fake is the contract every higher-level test depends on, so its
behavior (response shape, call recording, overrides) is locked here before
anything else in the LLM layer.
"""

from __future__ import annotations

from caliber.llm.fake import FakeLLMProvider
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    EvidenceContext,
    LLMUsage,
    PromptCandidate,
)


def _evidence(**overrides: object) -> EvidenceContext:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "item_id": "FB-1",
        "category": "hallucination",
        "severity": "critical",
        "free_text": "...",
    }
    defaults.update(overrides)
    return EvidenceContext(**defaults)  # type: ignore[arg-type]


def test_default_diagnose_returns_valid_diagnosis() -> None:
    provider = FakeLLMProvider()
    diagnosis, usage = provider.diagnose(_evidence())

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.root_cause  # non-empty
    assert 0.0 <= diagnosis.confidence <= 1.0
    assert isinstance(usage, LLMUsage)
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0


def test_records_calls_in_order() -> None:
    provider = FakeLLMProvider()
    e1 = _evidence(item_id="FB-1")
    e2 = _evidence(item_id="FB-2")
    provider.diagnose(e1)
    provider.diagnose(e2)
    assert [c.item_id for c in provider.calls] == ["FB-1", "FB-2"]


def test_diagnose_response_override() -> None:
    canned = Diagnosis(root_cause="overridden", confidence=0.42, affected_components=["prompt"])
    provider = FakeLLMProvider(diagnose_response=canned)
    diagnosis, _ = provider.diagnose(_evidence())
    assert diagnosis.root_cause == "overridden"
    assert diagnosis.confidence == 0.42


def test_diagnose_usage_override() -> None:
    provider = FakeLLMProvider(
        diagnose_usage=LLMUsage(input_tokens=1, output_tokens=2, cost_usd=3.0)
    )
    _, usage = provider.diagnose(_evidence())
    assert usage.input_tokens == 1
    assert usage.output_tokens == 2
    assert usage.cost_usd == 3.0


def test_diagnose_callable_override_sees_evidence() -> None:
    """A test can vary the response based on input by injecting a callable."""

    def callable_fn(evidence: EvidenceContext) -> tuple[Diagnosis, LLMUsage]:
        return (
            Diagnosis(root_cause=f"saw item={evidence.item_id}", confidence=0.9),
            LLMUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
        )

    provider = FakeLLMProvider(diagnose_callable=callable_fn)
    diagnosis, _ = provider.diagnose(_evidence(item_id="FB-42"))
    assert diagnosis.root_cause == "saw item=FB-42"


# ---------------------------------------------------------------------------
# generate_candidate
# ---------------------------------------------------------------------------


def _candidate_context(**overrides: object) -> CandidateContext:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "job_id": "RFN-1",
        "artifact_type": "prompt",
        "optimizer_type": "MetaPrompt",
        "diagnosis": Diagnosis(root_cause="missing tool call", confidence=0.8),
        "current_artifact_content": "You are a helpful agent.",
    }
    defaults.update(overrides)
    return CandidateContext(**defaults)  # type: ignore[arg-type]


def test_default_generate_candidate_returns_valid_candidate() -> None:
    provider = FakeLLMProvider()
    candidate, usage = provider.generate_candidate(_candidate_context())

    assert isinstance(candidate, PromptCandidate)
    assert candidate.artifact_type == "prompt"
    assert candidate.content  # non-empty
    assert isinstance(usage, LLMUsage)
    assert usage.input_tokens > 0


def test_records_candidate_calls() -> None:
    provider = FakeLLMProvider()
    c1 = _candidate_context(job_id="RFN-1")
    c2 = _candidate_context(job_id="RFN-2")
    provider.generate_candidate(c1)
    provider.generate_candidate(c2)
    assert [c.job_id for c in provider.candidate_calls] == ["RFN-1", "RFN-2"]


def test_default_candidate_references_diagnosis() -> None:
    """The fake's default body weaves the diagnosis into the candidate so
    integration tests can verify the wiring without mocking."""
    provider = FakeLLMProvider()
    ctx = _candidate_context(
        diagnosis=Diagnosis(root_cause="very specific root cause", confidence=0.9)
    )
    candidate, _ = provider.generate_candidate(ctx)
    assert "very specific root cause" in candidate.content


def test_default_candidate_handles_cold_start() -> None:
    """When ``current_artifact_content`` is None the fake bootstraps from scratch."""
    provider = FakeLLMProvider()
    ctx = _candidate_context(current_artifact_content=None)
    candidate, _ = provider.generate_candidate(ctx)
    assert candidate.content
    assert candidate.artifact_type == "prompt"


def test_candidate_response_override() -> None:
    canned = PromptCandidate(
        artifact_type="prompt",
        content="hand-rolled candidate",
        rationale="test",
        diff_summary="+0 / -0 lines",
    )
    provider = FakeLLMProvider(candidate_response=canned)
    candidate, _ = provider.generate_candidate(_candidate_context())
    assert candidate.content == "hand-rolled candidate"


def test_candidate_callable_override_sees_context() -> None:
    def callable_fn(ctx: CandidateContext) -> tuple[PromptCandidate, LLMUsage]:
        return (
            PromptCandidate(
                artifact_type=ctx.artifact_type,
                content=f"candidate for {ctx.job_id}",
                rationale="callable",
                diff_summary="+1 / -0",
            ),
            LLMUsage(input_tokens=1, output_tokens=2, cost_usd=0.03),
        )

    provider = FakeLLMProvider(candidate_callable=callable_fn)
    candidate, usage = provider.generate_candidate(_candidate_context(job_id="RFN-XYZ"))
    assert candidate.content == "candidate for RFN-XYZ"
    assert usage.cost_usd == 0.03


def test_calls_alias_backwards_compatible() -> None:
    """``provider.calls`` (the pre-2.8 attribute) still surfaces diagnose calls."""
    provider = FakeLLMProvider()
    provider.diagnose(_evidence(item_id="FB-X"))
    assert len(provider.calls) == 1
    assert provider.calls[0].item_id == "FB-X"
