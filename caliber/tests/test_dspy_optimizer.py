"""Tests for the DSPyBootstrapFewShot optimizer handler.

Covers the four seams the DSPy integration adds:

* selector routing (explicit override + the gated auto-rule),
* ``run_candidate`` trainset loading + context wiring (via the fake provider),
* the OpenAI provider's dispatch + empty-trainset MetaPrompt fallback,
* a real end-to-end BootstrapFewShot run over ``dspy``'s ``DummyLM`` (no
  network), and the MIPRO wrapper via a stubbed ``compile``, and
* the pure helpers in :mod:`caliber.llm.dspy_optimizer`.

``dspy`` ships in the dedicated ``[dspy]`` extra, so these tests skip cleanly
when that extra is not installed in the active environment.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

dspy = pytest.importorskip("dspy")

from caliber.artifact_store import FakeArtifactStore
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.llm import openai_agents
from caliber.llm.dspy_optimizer import (
    EmptyTrainsetError,
    _attr_or_key,
    _demo_metric,
    _example_text,
    _extract_demos,
    _extract_instruction,
    _normalize,
    _render_demos_block,
    _usage_from_history,
    run_bootstrap_fewshot,
    run_mipro,
)
from caliber.llm.fake import FakeLLMProvider
from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    LLMProviderError,
    LLMUsage,
    PromptCandidate,
)
from caliber.orchestrator.candidate import _load_trainset, run_candidate
from caliber.orchestrator.optimizer_select import select_optimizer

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _ctx(
    *,
    optimizer_type: str = "DSPyBootstrapFewShot",
    trainset: list[dict[str, object]] | None = None,
    current: str | None = "You are a helpful support agent.",
) -> CandidateContext:
    return CandidateContext(
        agent_id="support-agent",
        job_id="RFN-D",
        artifact_type="prompt",
        optimizer_type=optimizer_type,
        diagnosis=Diagnosis(root_cause="needs worked examples", confidence=0.9),
        current_artifact_content=current,
        trainset=trainset,
    )


def _seed_agent_and_job(
    session: Session,
    *,
    optimizer_config: dict[str, object] | None = None,
    eval_thresholds: dict[str, object] | None = None,
    diagnosis: dict[str, object] | None = None,
    artifact_type: str = "prompt",
) -> tuple[CaliberAgentConfig, CaliberRefinementJob]:
    agent = CaliberAgentConfig(
        agent_id="support-agent",
        experiment_id="exp",
        name="Support",
        owner="@sarah",
        artifact_types=["prompt"],
        eval_thresholds=eval_thresholds or {},
        optimizer_config=optimizer_config or {},
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-D",
            agent_id="support-agent",
            category="hallucination",
            free_text="Agent invented a refund timeline.",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-D",
        agent_id="support-agent",
        primary_item_id="FB-D",
        artifact_type=artifact_type,
        status="running",
        current_stage="candidate",
        bundle_targets=[],
        diagnosis=diagnosis
        or {
            "root_cause": "Prompt would benefit from few-shot examples.",
            "affected_components": ["prompt"],
            "confidence": 0.85,
            "alternatives": [],
        },
    )
    session.add(job)
    session.commit()
    return agent, job


def _seed_dataset(session: Session, *, dataset_id: str, name: str, n: int = 2) -> None:
    session.add(CaliberEvalDataset(dataset_id=dataset_id, name=name, owner="@sarah", version=1))
    session.flush()
    for i in range(n):
        session.add(
            CaliberEvalDatasetExample(
                example_id=f"{dataset_id}-ex-{i}",
                dataset_id=dataset_id,
                dataset_version=1,
                input={"input": f"question {i}"},
                expected={"expected": f"answer {i}"},
                weight=1.0,
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


def test_selector_explicit_override_wins(db_session: Session) -> None:
    agent, job = _seed_agent_and_job(db_session, optimizer_config={"type": "DSPyBootstrapFewShot"})
    assert select_optimizer(agent, job) == "DSPyBootstrapFewShot"


def test_selector_explicit_override_mipro(db_session: Session) -> None:
    """DSPyMIPRO is override-only (no auto-rule, since it is the most expensive)."""
    agent, job = _seed_agent_and_job(db_session, optimizer_config={"type": "DSPyMIPRO"})
    assert select_optimizer(agent, job) == "DSPyMIPRO"


def test_selector_auto_fires_when_opted_in(db_session: Session) -> None:
    agent, job = _seed_agent_and_job(
        db_session,
        optimizer_config={"dspy_fewshot_auto": True},
        diagnosis={
            "root_cause": "Model needs few-shot exemplars to format answers.",
            "confidence": 0.85,
            "alternatives": [],
        },
    )
    assert select_optimizer(agent, job) == "DSPyBootstrapFewShot"


def test_selector_auto_off_by_default(db_session: Session) -> None:
    """Without the opt-in flag, a few-shot diagnosis still routes to MetaPrompt."""
    agent, job = _seed_agent_and_job(
        db_session,
        optimizer_config={},
        diagnosis={
            "root_cause": "Model needs few-shot exemplars.",
            "confidence": 0.85,
            "alternatives": [],
        },
    )
    assert select_optimizer(agent, job) == "MetaPrompt"


def test_selector_auto_skipped_for_skill_jobs(db_session: Session) -> None:
    agent, job = _seed_agent_and_job(
        db_session,
        optimizer_config={"dspy_fewshot_auto": True},
        artifact_type="skill",
        diagnosis={
            "root_cause": "Skill needs few-shot examples.",
            "confidence": 0.85,
            "alternatives": [],
        },
    )
    assert select_optimizer(agent, job) == "SkillMetaPrompt"


def test_selector_auto_skipped_without_fewshot_language(db_session: Session) -> None:
    agent, job = _seed_agent_and_job(
        db_session,
        optimizer_config={"dspy_fewshot_auto": True},
        diagnosis={
            "root_cause": "Prompt allows skipping the lookup tool.",
            "confidence": 0.85,
            "alternatives": [],
        },
    )
    assert select_optimizer(agent, job) == "MetaPrompt"


# ---------------------------------------------------------------------------
# _load_trainset
# ---------------------------------------------------------------------------


def test_load_trainset_by_name(db_session: Session) -> None:
    agent, _job = _seed_agent_and_job(db_session, eval_thresholds={"eval_dataset_id": "support-ds"})
    _seed_dataset(db_session, dataset_id="DS-1", name="support-ds", n=3)

    trainset = _load_trainset(db_session, agent)

    assert len(trainset) == 3
    assert trainset[0] == {
        "input": {"input": "question 0"},
        "expected": {"expected": "answer 0"},
        "weight": 1.0,
    }


def test_load_trainset_by_id(db_session: Session) -> None:
    agent, _job = _seed_agent_and_job(db_session, eval_thresholds={"eval_dataset_id": "DS-2"})
    _seed_dataset(db_session, dataset_id="DS-2", name="some-name", n=1)

    trainset = _load_trainset(db_session, agent)
    assert len(trainset) == 1


def test_load_trainset_empty_when_no_dataset(db_session: Session) -> None:
    agent, _job = _seed_agent_and_job(db_session, eval_thresholds={"eval_dataset_id": "missing"})
    assert _load_trainset(db_session, agent) == []


def test_load_trainset_skips_superseded_examples(db_session: Session) -> None:
    agent, _job = _seed_agent_and_job(db_session, eval_thresholds={"eval_dataset_id": "DS-3"})
    _seed_dataset(db_session, dataset_id="DS-3", name="ds3", n=2)
    example = db_session.get(CaliberEvalDatasetExample, "DS-3-ex-0")
    assert example is not None
    from datetime import datetime, timezone

    example.superseded_at = datetime.now(timezone.utc)
    db_session.commit()

    assert len(_load_trainset(db_session, agent)) == 1


# ---------------------------------------------------------------------------
# run_candidate (end-to-end via the fake provider)
# ---------------------------------------------------------------------------


def test_run_candidate_dspy_loads_trainset_and_marks_candidate(db_session: Session) -> None:
    _seed_agent_and_job(
        db_session,
        optimizer_config={"type": "DSPyBootstrapFewShot"},
        eval_thresholds={"eval_dataset_id": "support-ds"},
    )
    _seed_dataset(db_session, dataset_id="DS-X", name="support-ds", n=2)

    job = run_candidate(db_session, "RFN-D", FakeLLMProvider(), FakeArtifactStore())

    assert job.current_stage == "eval"
    assert job.optimizer_type == "DSPyBootstrapFewShot"
    content = job.candidate["content"]
    assert "DSPyBootstrapFewShot" in content
    # The fake echoes the trainset size, proving run_candidate loaded + threaded it.
    assert "Trainset examples available: 2" in content


def test_run_candidate_dspy_with_no_dataset_still_advances(db_session: Session) -> None:
    """No eval dataset → empty trainset → still produces a (fallback) candidate."""
    _seed_agent_and_job(db_session, optimizer_config={"type": "DSPyBootstrapFewShot"})
    job = run_candidate(db_session, "RFN-D", FakeLLMProvider(), FakeArtifactStore())

    assert job.current_stage == "eval"
    assert job.optimizer_type == "DSPyBootstrapFewShot"
    assert "Trainset examples available: 0" in job.candidate["content"]


def test_run_candidate_mipro_loads_trainset_and_marks_candidate(db_session: Session) -> None:
    _seed_agent_and_job(
        db_session,
        optimizer_config={"type": "DSPyMIPRO", "dspy_mipro_auto": "medium"},
        eval_thresholds={"eval_dataset_id": "support-ds"},
    )
    _seed_dataset(db_session, dataset_id="DS-M", name="support-ds", n=3)

    job = run_candidate(db_session, "RFN-D", FakeLLMProvider(), FakeArtifactStore())

    assert job.current_stage == "eval"
    assert job.optimizer_type == "DSPyMIPRO"
    content = job.candidate["content"]
    assert "DSPyMIPRO" in content
    assert "Trainset examples available: 3" in content
    # The per-agent MIPRO budget override flowed through to the context.
    assert "MIPRO budget preset: medium" in content


# ---------------------------------------------------------------------------
# OpenAI provider dispatch + empty-trainset fallback
# ---------------------------------------------------------------------------


def _provider() -> OpenAIAgentsLLMProvider:
    return OpenAIAgentsLLMProvider(
        api_key="sk-test",
        diagnosis_model="gpt-4o-mini",
        allow_flagged_dspy_optimizers=True,
    )


def test_provider_dspy_empty_trainset_falls_back_to_metaprompt(monkeypatch) -> None:
    provider = _provider()
    sentinel = (
        PromptCandidate(artifact_type="prompt", content="META-FALLBACK", rationale=""),
        LLMUsage(),
    )
    monkeypatch.setattr(provider, "_generate_candidate_metaprompt", lambda ctx: sentinel)

    candidate, _usage = provider.generate_candidate(_ctx(trainset=None))
    assert candidate.content == "META-FALLBACK"


@pytest.mark.parametrize("optimizer", ["DSPyBootstrapFewShot", "DSPyMIPRO"])
def test_provider_dspy_empty_trainset_falls_back_per_optimizer(monkeypatch, optimizer: str) -> None:
    provider = _provider()
    sentinel = (
        PromptCandidate(artifact_type="prompt", content="META-FALLBACK", rationale=""),
        LLMUsage(),
    )
    monkeypatch.setattr(provider, "_generate_candidate_metaprompt", lambda ctx: sentinel)

    candidate, _usage = provider.generate_candidate(_ctx(optimizer_type=optimizer, trainset=None))
    assert candidate.content == "META-FALLBACK"


def test_provider_unknown_optimizer_raises(monkeypatch) -> None:
    provider = _provider()
    # Guard against any accidental network by stubbing the metaprompt path too.
    monkeypatch.setattr(
        provider,
        "_generate_candidate_metaprompt",
        lambda ctx: (_ for _ in ()).throw(AssertionError("should not be reached")),
    )
    with pytest.raises(LLMProviderError, match="not implemented"):
        provider.generate_candidate(_ctx(optimizer_type="TextGrad"))


def test_provider_dspy_runtime_advisory_falls_back_to_metaprompt(monkeypatch) -> None:
    provider = OpenAIAgentsLLMProvider(
        api_key="sk-test",
        diagnosis_model="gpt-4o-mini",
        allow_flagged_dspy_optimizers=False,
    )
    sentinel = (
        PromptCandidate(
            artifact_type="prompt",
            content="META-FALLBACK",
            rationale="Keeps the prompt minimal.",
            diff_summary="+1 / -0",
        ),
        LLMUsage(),
    )
    monkeypatch.setattr(provider, "_generate_candidate_metaprompt", lambda ctx: sentinel)
    monkeypatch.setattr(
        openai_agents,
        "dspy_optimizer_block_reason",
        lambda *, allow_flagged=False: None if allow_flagged else "DSPy stack is blocked",
    )

    candidate, _usage = provider.generate_candidate(
        _ctx(
            optimizer_type="DSPyBootstrapFewShot",
            trainset=[{"input": "question", "expected": "answer"}],
        )
    )

    assert candidate.content == "META-FALLBACK"
    assert "DSPy stack is blocked" in candidate.rationale
    assert "DSPyBootstrapFewShot -> MetaPrompt fallback" in candidate.diff_summary


# ---------------------------------------------------------------------------
# Real bridge runs (dspy DummyLM — no network)
# ---------------------------------------------------------------------------


def test_run_bootstrap_fewshot_end_to_end_with_dummy_lm(monkeypatch) -> None:
    """Drive the real BootstrapFewShot teleprompter with dspy's DummyLM.

    DummyLM always answers "blue"; both trainset examples expect "blue", so the
    metric keeps both bootstrapped demos and they land in the rendered prompt.
    """
    from dspy.utils.dummies import DummyLM

    lm = DummyLM([{"answer": "blue"}] * 20)
    monkeypatch.setattr(dspy, "LM", lambda *args, **kwargs: lm)

    context = _ctx(
        optimizer_type="DSPyBootstrapFewShot",
        trainset=[
            {"input": "What color is the sky?", "expected": "blue"},
            {"input": "What color is the ocean?", "expected": "blue"},
        ],
        current="Answer the question concisely.",
    )
    candidate, _usage = run_bootstrap_fewshot(
        context=context, model="gpt-4o-mini", max_bootstrapped_demos=2, max_labeled_demos=2
    )

    assert candidate.artifact_type == "prompt"
    assert candidate.content.startswith("Answer the question concisely.")
    assert "Few-shot examples (selected by DSPy BootstrapFewShot)" in candidate.content
    assert "What color is the sky?" in candidate.content
    assert "few-shot demo(s)" in candidate.diff_summary


def test_run_mipro_wrapper_with_stubbed_compile(monkeypatch) -> None:
    """Exercise run_mipro's wrapper + _build_candidate without MIPRO's
    LM-bound instruction search (which DummyLM can't satisfy).

    Stub ``MIPROv2.compile`` to return a compiled program whose predictor
    carries an optimized instruction + a demo, then assert the candidate
    reflects the rewritten instruction and the few-shot block.
    """
    from dspy.teleprompt import MIPROv2
    from dspy.utils.dummies import DummyLM

    monkeypatch.setattr(dspy, "LM", lambda *args, **kwargs: DummyLM([{"answer": "x"}]))

    fake_predictor = SimpleNamespace(
        signature=SimpleNamespace(instructions="OPTIMIZED INSTRUCTION BODY"),
        demos=[SimpleNamespace(question="q1", answer="a1")],
    )
    fake_compiled = SimpleNamespace(demos=[], predictors=lambda: [fake_predictor])
    monkeypatch.setattr(MIPROv2, "compile", lambda self, student, **kwargs: fake_compiled)

    context = _ctx(
        optimizer_type="DSPyMIPRO",
        trainset=[{"input": "q", "expected": "a"}],
        current="Original instruction.",
    )
    candidate, _usage = run_mipro(
        context=context,
        model="gpt-4o-mini",
        max_bootstrapped_demos=2,
        max_labeled_demos=2,
        auto="light",
    )

    assert candidate.content.startswith("OPTIMIZED INSTRUCTION BODY")
    assert "Few-shot examples" in candidate.content
    assert "instruction rewritten" in candidate.diff_summary


# ---------------------------------------------------------------------------
# Pure helpers (no dspy required)
# ---------------------------------------------------------------------------


def test_example_text_variants() -> None:
    assert _example_text(None) == ""
    assert _example_text("  hi  ") == "hi"
    assert _example_text({"input": "x"}) == "x"
    assert _example_text({"question": "q"}) == "q"
    assert _example_text({"foo": "bar"}) == '{"foo": "bar"}'
    assert _example_text(123) == "123"


def test_render_demos_block() -> None:
    assert _render_demos_block([]) == ""
    block = _render_demos_block([{"question": "q1", "answer": "a1"}])
    assert "Few-shot examples (selected by DSPy BootstrapFewShot)" in block
    assert "## Example 1" in block
    assert "Input: q1" in block
    assert "Output: a1" in block


def test_demo_metric() -> None:
    gold = SimpleNamespace(answer="Refunds take 5 days")
    assert _demo_metric(gold, SimpleNamespace(answer="our refunds take 5 days now")) is True
    assert _demo_metric(gold, SimpleNamespace(answer="completely unrelated")) is False
    assert _demo_metric(SimpleNamespace(answer=""), SimpleNamespace(answer="x")) is True
    assert _demo_metric(gold, SimpleNamespace(answer="")) is False


def test_extract_demos_from_demos_attr() -> None:
    compiled = SimpleNamespace(demos=[SimpleNamespace(question="q", answer="a")])
    assert _extract_demos(compiled) == [{"question": "q", "answer": "a"}]


def test_extract_demos_from_predictors_fallback() -> None:
    predictor = SimpleNamespace(demos=[SimpleNamespace(question="q1", answer="a1")])
    compiled = SimpleNamespace(demos=[], predictors=lambda: [predictor])
    assert _extract_demos(compiled) == [{"question": "q1", "answer": "a1"}]


def test_extract_demos_empty() -> None:
    compiled = SimpleNamespace(demos=[], predictors=lambda: [])
    assert _extract_demos(compiled) == []


def test_extract_instruction_from_predictor_signature() -> None:
    predictor = SimpleNamespace(signature=SimpleNamespace(instructions="OPTIMIZED INSTRUCTION"))
    compiled = SimpleNamespace(predictors=lambda: [predictor])
    assert _extract_instruction(compiled, "original") == "OPTIMIZED INSTRUCTION"


def test_extract_instruction_falls_back() -> None:
    compiled = SimpleNamespace(predictors=lambda: [SimpleNamespace(signature=None)])
    assert _extract_instruction(compiled, "original") == "original"


def test_usage_from_history() -> None:
    lm = SimpleNamespace(
        history=[
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            {"no_usage": True},
        ]
    )
    usage = _usage_from_history(lm)
    assert usage.input_tokens == 13
    assert usage.output_tokens == 7
    assert usage.cost_usd == 0.0

    assert _usage_from_history(SimpleNamespace(history=None)).input_tokens == 0


def test_attr_or_key() -> None:
    assert _attr_or_key({"question": "q"}, "question") == "q"
    assert _attr_or_key(SimpleNamespace(answer="a"), "answer") == "a"
    assert _attr_or_key(SimpleNamespace(), "missing") == ""


def test_normalize() -> None:
    assert _normalize("  Hi There  ") == "hi there"
    assert _normalize(5) == "5"


def test_empty_trainset_error_is_runtimeerror() -> None:
    assert issubclass(EmptyTrainsetError, RuntimeError)
