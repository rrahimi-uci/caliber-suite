"""Focused tests for the OpenAI Agents-backed LLM provider.

The provider is intentionally lazy around optional packages. These tests use
small fake modules so the production branches are covered without installing or
calling the real OpenAI Agents SDK, MLflow GenAI optimizer, or OpenAI API.
"""

from __future__ import annotations

import builtins
import json
import sys
import types
from types import SimpleNamespace

import pytest

from caliber.llm import openai_agents
from caliber.llm.openai_agents import (
    OpenAIAgentsLLMProvider,
    _extract_output,
    _extract_usage,
    _normalize_reflection_model,
)
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    EvidenceContext,
    LLMProviderError,
    LLMUsage,
    PromptCandidate,
    WorkflowEditContext,
    WorkflowGenerationContext,
)


def _diagnosis() -> Diagnosis:
    return Diagnosis(
        root_cause="The prompt never requires a policy lookup.",
        affected_components=["prompt"],
        confidence=0.82,
        alternatives=["Tool outage"],
    )


def _provider(**overrides: object) -> OpenAIAgentsLLMProvider:
    return OpenAIAgentsLLMProvider(
        api_key="sk-test",
        diagnosis_model="gpt-4o-mini",
        **overrides,
    )


def _install_agents_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: object | None = None,
    error: Exception | None = None,
) -> tuple[type[object], type[object]]:
    class FakeAgent:
        instances: list[FakeAgent] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            FakeAgent.instances.append(self)

    class FakeRunner:
        calls: list[dict[str, object]] = []

        @staticmethod
        def run_sync(agent: object, prompt: str) -> object:
            FakeRunner.calls.append({"agent": agent, "prompt": prompt})
            if error is not None:
                raise error
            return result

    agents_mod = types.ModuleType("agents")
    agents_mod.Agent = FakeAgent
    agents_mod.Runner = FakeRunner
    monkeypatch.setitem(sys.modules, "agents", agents_mod)
    return FakeAgent, FakeRunner


def _candidate_context(**overrides: object) -> CandidateContext:
    values: dict[str, object] = {
        "agent_id": "support-agent",
        "job_id": "RFN-1",
        "artifact_type": "prompt",
        "optimizer_type": "MetaPrompt",
        "diagnosis": _diagnosis(),
        "current_artifact_content": "You are a concise support assistant.",
    }
    values.update(overrides)
    return CandidateContext(**values)  # type: ignore[arg-type]


def test_diagnose_builds_agent_once_and_accepts_dict_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        final_output={
            "root_cause": "The agent skipped the required lookup.",
            "affected_components": ["prompt"],
            "confidence": 0.9,
            "alternatives": [],
        },
        usage={"prompt_tokens": 11, "completion_tokens": 7, "cost_usd": 0.03},
    )
    fake_agent, fake_runner = _install_agents_module(monkeypatch, result=result)

    provider = _provider()
    evidence = EvidenceContext(
        agent_id="support-agent",
        item_id="FB-1",
        category="policy",
        severity="high",
        free_text="Customer received the wrong policy answer.",
        trace_id="trace-1",
        session_id="session-1",
        evidence_summary={"tool_calls": ["lookup_policy"]},
    )

    diagnosis, usage = provider.diagnose(evidence)

    assert diagnosis.root_cause == "The agent skipped the required lookup."
    assert usage.input_tokens == 11
    assert usage.output_tokens == 7
    assert usage.cost_usd == 0.03
    assert len(fake_agent.instances) == 1
    assert provider._ensure_diagnosis_agent() is fake_agent.instances[0]
    assert fake_agent.instances[0].kwargs["name"] == "caliber.diagnosis"
    assert fake_agent.instances[0].kwargs["output_type"] is Diagnosis
    prompt = fake_runner.calls[0]["prompt"]
    assert '"trace_id": "trace-1"' in prompt
    assert '"tool_calls": [' in prompt


def test_generate_candidate_builds_metaprompt_agent_and_includes_review_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = PromptCandidate(
        artifact_type="prompt",
        content="Always call lookup_policy before answering.",
        rationale="Adds the missing tool-use requirement.",
        diff_summary="+1 / -0",
    )
    result = SimpleNamespace(
        final_output=candidate,
        usage=SimpleNamespace(input_tokens=5, output_tokens=9, cost=0.12),
    )
    fake_agent, fake_runner = _install_agents_module(monkeypatch, result=result)

    provider = _provider()
    generated, usage = provider.generate_candidate(
        _candidate_context(review_notes="Keep the existing tone.")
    )

    assert generated is candidate
    assert usage.input_tokens == 5
    assert usage.output_tokens == 9
    assert usage.cost_usd == 0.12
    assert len(fake_agent.instances) == 1
    assert provider._ensure_candidate_agent() is fake_agent.instances[0]
    assert fake_agent.instances[0].kwargs["name"] == "caliber.candidate.metaprompt"
    assert fake_agent.instances[0].kwargs["output_type"] is PromptCandidate
    prompt = fake_runner.calls[0]["prompt"]
    assert "Reviewer guidance" in prompt
    assert "Keep the existing tone." in prompt
    assert "You are a concise support assistant." in prompt


def test_generate_candidate_dspy_falls_back_when_dspy_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
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
        "_load_dspy_optimizer_bridge",
        lambda: (_ for _ in ()).throw(
            LLMProviderError(
                "DSPy optimizer dependencies are not installed. Install with `pip install caliber[dspy]` to enable DSPy refinement paths."
            )
        ),
    )

    candidate, _usage = provider.generate_candidate(
        _candidate_context(
            optimizer_type="DSPyBootstrapFewShot", trainset=[{"input": "Q", "expected": "A"}]
        )
    )

    assert candidate.content == "META-FALLBACK"
    assert "Install with `pip install caliber[dspy]`" in candidate.rationale
    assert "DSPyBootstrapFewShot -> MetaPrompt fallback" in candidate.diff_summary


def _edit_context(**overrides: object) -> WorkflowEditContext:
    values: dict[str, object] = {
        "instruction": "add a PII guardrail after the agent",
        "manifest": {"schema_version": 1, "workflow_id": "wf", "nodes": {}},
        "grounding": {"tools": ["lookup_policy"], "skills": [], "eval_datasets": []},
    }
    values.update(overrides)
    return WorkflowEditContext(**values)  # type: ignore[arg-type]


def test_propose_workflow_edit_parses_manifest_json_and_grounds_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposed = {"schema_version": 1, "workflow_id": "wf", "nodes": {"start": {"type": "start"}}}
    result = SimpleNamespace(
        final_output={
            "manifest_json": json.dumps(proposed),
            "summary": "Add a guardrail",
            "rationale": "safety",
        },
        usage={"prompt_tokens": 20, "completion_tokens": 8, "cost_usd": 0.05},
    )
    fake_agent, fake_runner = _install_agents_module(monkeypatch, result=result)

    provider = _provider()
    edit, usage = provider.propose_workflow_edit(_edit_context())

    assert edit.manifest == proposed
    assert edit.summary == "Add a guardrail"
    assert edit.rationale == "safety"
    assert usage.input_tokens == 20
    # Agent wiring + grounding/instruction reach the prompt.
    assert fake_agent.instances[0].kwargs["name"] == "caliber.workflow.copilot"
    prompt = fake_runner.calls[0]["prompt"]
    assert "add a PII guardrail after the agent" in prompt
    assert "lookup_policy" in prompt


def test_propose_workflow_edit_raises_on_non_json_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        final_output={"manifest_json": "this is not json{", "summary": "", "rationale": ""},
        usage={},
    )
    _install_agents_module(monkeypatch, result=result)
    provider = _provider()
    with pytest.raises(LLMProviderError, match="non-JSON manifest"):
        provider.propose_workflow_edit(_edit_context())


def test_propose_workflow_edit_raises_when_manifest_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        final_output={"manifest_json": json.dumps([1, 2, 3]), "summary": "", "rationale": ""},
        usage={},
    )
    _install_agents_module(monkeypatch, result=result)
    provider = _provider()
    with pytest.raises(LLMProviderError, match="not a JSON object"):
        provider.propose_workflow_edit(_edit_context())


def _gen_context(**overrides: object) -> WorkflowGenerationContext:
    values: dict[str, object] = {
        "goal": "a 3-step support triage workflow",
        "manifest": {"schema_version": 1, "workflow_id": "wf", "nodes": {}},
        "grounding": {"tools": ["lookup_policy"], "skills": [], "eval_datasets": []},
    }
    values.update(overrides)
    return WorkflowGenerationContext(**values)  # type: ignore[arg-type]


def test_generate_workflow_from_goal_parses_manifest_json_and_grounds_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authored = {"schema_version": 1, "workflow_id": "wf", "nodes": {"start": {"type": "start"}}}
    result = SimpleNamespace(
        final_output={
            "manifest_json": json.dumps(authored),
            "summary": "3-step triage",
            "rationale": "satisfies the goal",
        },
        usage={"prompt_tokens": 22, "completion_tokens": 9, "cost_usd": 0.06},
    )
    fake_agent, fake_runner = _install_agents_module(monkeypatch, result=result)

    provider = _provider()
    edit, usage = provider.generate_workflow_from_goal(_gen_context())

    assert edit.manifest == authored
    assert edit.summary == "3-step triage"
    assert usage.input_tokens == 22
    # Dedicated plan-build agent + the goal/grounding reach the prompt.
    assert fake_agent.instances[0].kwargs["name"] == "caliber.workflow.plan_build"
    prompt = fake_runner.calls[0]["prompt"]
    assert "a 3-step support triage workflow" in prompt
    assert "lookup_policy" in prompt


def test_generate_workflow_from_goal_raises_on_non_json_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        final_output={"manifest_json": "not json{", "summary": "", "rationale": ""},
        usage={},
    )
    _install_agents_module(monkeypatch, result=result)
    provider = _provider()
    with pytest.raises(LLMProviderError, match="non-JSON manifest"):
        provider.generate_workflow_from_goal(_gen_context())


def test_missing_openai_agents_sdk_raises_clear_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIAgentsLLMProvider.__new__(OpenAIAgentsLLMProvider)
    real_import = builtins.__import__

    def _import_without_agents(name: str, *args: object, **kwargs: object) -> object:
        if name == "agents":
            raise ImportError("agents package missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import_without_agents)

    with pytest.raises(LLMProviderError, match="openai-agents is not installed"):
        provider._agent_class()

    with pytest.raises(LLMProviderError, match="openai-agents is not installed"):
        provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")


def test_run_agent_sync_wraps_sdk_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_agents_module(monkeypatch, error=RuntimeError("rate limited"))
    provider = _provider()

    with pytest.raises(LLMProviderError, match="diagnosis LLM call failed: rate limited"):
        provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")


def test_extract_helpers_cover_sdk_shape_variants() -> None:
    candidate = _extract_output(
        SimpleNamespace(
            final_output={
                "artifact_type": "prompt",
                "content": "new prompt",
                "rationale": "fixed",
                "diff_summary": "+1 / -1",
            }
        ),
        PromptCandidate,
        stage="candidate",
    )
    assert candidate.content == "new prompt"

    with pytest.raises(LLMProviderError, match="unexpected output type: NoneType"):
        _extract_output(SimpleNamespace(), Diagnosis, stage="diagnosis")

    usage = _extract_usage(
        SimpleNamespace(token_usage={"input_tokens": "3", "output_tokens": 4, "cost": "0.25"})
    )
    assert usage.input_tokens == 3
    assert usage.output_tokens == 4
    assert usage.cost_usd == 0.25

    assert _extract_usage(SimpleNamespace()).input_tokens == 0
    assert _extract_usage(SimpleNamespace(usage=SimpleNamespace())).output_tokens == 0


def test_extract_usage_degrades_gracefully_on_non_numeric_fields() -> None:
    """Regression (#5): a non-numeric token/cost field must NOT raise (which
    previously leaked a bare ValueError out of a successful call, bypassing the
    LLMProviderError contract). Telemetry degrades to zeros instead."""
    # Bad token field → that field is 0, the good one survives, no exception.
    usage = _extract_usage(SimpleNamespace(usage={"input_tokens": "n/a", "output_tokens": 5}))
    assert usage.input_tokens == 0
    assert usage.output_tokens == 5
    # Bad cost field → cost 0.0, still no exception.
    usage = _extract_usage(SimpleNamespace(usage={"cost": "free"}))
    assert usage == LLMUsage()
    # Valid string values still coerce (existing SDK shapes feed strings).
    usage = _extract_usage(SimpleNamespace(usage={"input_tokens": "7", "cost": "0.5"}))
    assert usage.input_tokens == 7
    assert usage.cost_usd == 0.5


def _install_gepa_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    optimize_error: Exception | None = None,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    mlflow_mod = types.ModuleType("mlflow")
    genai_mod = types.ModuleType("mlflow.genai")
    genai_mod.__path__ = []  # type: ignore[attr-defined]
    optimize_pkg = types.ModuleType("mlflow.genai.optimize")
    optimize_pkg.__path__ = []  # type: ignore[attr-defined]
    optimizers_mod = types.ModuleType("mlflow.genai.optimize.optimizers")
    scorers_mod = types.ModuleType("mlflow.genai.scorers")

    class FakeGepaPromptOptimizer:
        def __init__(self, **kwargs: object) -> None:
            captured["optimizer_kwargs"] = kwargs

    class FakeCorrectness:
        def __init__(self, *, model: str) -> None:
            captured["correctness_model"] = model

    class FakeLoadedPrompt:
        def format(self, **kwargs: object) -> str:
            captured["format_kwargs"] = kwargs
            return f"formatted {kwargs['task']}"

    def register_prompt(**kwargs: object) -> object:
        captured["register_prompt_kwargs"] = kwargs
        return SimpleNamespace(uri="prompts:/caliber-gepa/1")

    def load_prompt(name: str) -> FakeLoadedPrompt:
        captured["loaded_prompt_name"] = name
        return FakeLoadedPrompt()

    def optimize_prompts(**kwargs: object) -> object:
        captured["optimize_prompts_kwargs"] = kwargs
        predict_fn = kwargs["predict_fn"]
        assert callable(predict_fn)
        captured["predict_output"] = predict_fn(task="resolve issue")
        if optimize_error is not None:
            raise optimize_error
        return SimpleNamespace(
            optimized_prompts=[SimpleNamespace(template="optimized GEPA prompt")],
            initial_eval_score=0.25,
            final_eval_score=0.88,
        )

    genai_mod.register_prompt = register_prompt
    genai_mod.load_prompt = load_prompt
    genai_mod.optimize_prompts = optimize_prompts
    mlflow_mod.genai = genai_mod
    optimizers_mod.GepaPromptOptimizer = FakeGepaPromptOptimizer
    scorers_mod.Correctness = FakeCorrectness

    openai_mod = types.ModuleType("openai")

    class FakeOpenAI:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, *, model: str, messages: list[dict[str, str]]) -> object:
            captured["openai_request"] = {"model": model, "messages": messages}
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="predicted answer"))]
            )

    openai_mod.OpenAI = FakeOpenAI

    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "mlflow.genai.optimize", optimize_pkg)
    monkeypatch.setitem(sys.modules, "mlflow.genai.optimize.optimizers", optimizers_mod)
    monkeypatch.setitem(sys.modules, "mlflow.genai.scorers", scorers_mod)
    monkeypatch.setitem(sys.modules, "openai", openai_mod)
    return captured


def test_gepa_uses_configured_models_and_metric_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_gepa_modules(monkeypatch)
    provider = _provider(
        gepa_reflection_model="gpt-4.1-mini",
        gepa_max_metric_calls=42,
    )

    candidate, usage = provider.generate_candidate(
        _candidate_context(
            artifact_type="skill",
            optimizer_type="GEPA",
            current_artifact_content=None,
            skill_name="triage-skill",
        )
    )

    assert candidate.content == "optimized GEPA prompt"
    assert "0.25" in candidate.rationale
    assert "0.88" in candidate.rationale
    assert usage.input_tokens == 0
    assert captured["optimizer_kwargs"] == {
        "reflection_model": "openai:/gpt-4.1-mini",
        "max_metric_calls": 42,
        "display_progress_bar": False,
    }
    assert captured["correctness_model"] == "openai:/gpt-4.1-mini"
    assert captured["register_prompt_kwargs"] == {
        "name": "caliber-gepa-RFN-1",
        "template": "You are triage-skill.",
        "commit_message": "GEPA baseline for job RFN-1",
    }
    assert captured["loaded_prompt_name"] == "caliber-gepa-RFN-1"
    assert captured["format_kwargs"] == {"task": "resolve issue"}
    assert captured["predict_output"] == "predicted answer"
    assert captured["openai_request"] == {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": "formatted resolve issue"}],
    }
    optimize_kwargs = captured["optimize_prompts_kwargs"]
    assert optimize_kwargs["prompt_uris"] == ["prompts:/caliber-gepa/1"]
    assert optimize_kwargs["enable_tracking"] is True


def test_gepa_wraps_optimizer_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_gepa_modules(monkeypatch, optimize_error=RuntimeError("optimizer broke"))
    provider = _provider()

    with pytest.raises(LLMProviderError, match="GEPA optimization failed: optimizer broke"):
        provider.generate_candidate(_candidate_context(optimizer_type="GEPA"))


def test_reflection_model_normalization() -> None:
    assert _normalize_reflection_model("gpt-4o") == "openai:/gpt-4o"
    assert _normalize_reflection_model("openai:/gpt-4o-mini") == "openai:/gpt-4o-mini"
