"""Focused tests for the OpenAI Agents-backed LLM provider.

The provider is intentionally lazy around optional packages. These tests use
small fake modules so the production branches are covered without installing or
calling the real OpenAI Agents SDK, MLflow GenAI optimizer, or OpenAI API.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import types
from types import SimpleNamespace

import httpx
import openai
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


def test_provider_sets_api_key_and_disables_agents_tracing_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_AGENTS_DISABLE_TRACING", raising=False)

    _provider()

    assert os.environ["OPENAI_API_KEY"] == "sk-test"
    assert os.environ["OPENAI_AGENTS_DISABLE_TRACING"] == "1"


def test_provider_preserves_explicit_tracing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "0")

    _provider()

    assert os.environ["OPENAI_AGENTS_DISABLE_TRACING"] == "0"


def _install_agents_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: object | None = None,
    error: Exception | None = None,
    errors: list[Exception | None] | None = None,
) -> tuple[type[object], type[object]]:
    """Fake the ``agents`` package.

    ``error`` raises the same exception on every call (existing tests use
    this for "always fails"). ``errors`` scripts one outcome per call --
    ``None`` means "succeed with ``result`` on this attempt" -- so a retry
    test can say e.g. ``[TimeoutError(...), None]`` for "fails once, then
    succeeds"; the last entry repeats once the list is exhausted.
    """

    class FakeAgent:
        instances: list[FakeAgent] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            FakeAgent.instances.append(self)

    class FakeRunner:
        calls: list[dict[str, object]] = []

        @staticmethod
        def run_sync(agent: object, prompt: str, **kwargs: object) -> object:
            FakeRunner.calls.append({"agent": agent, "prompt": prompt, **kwargs})
            if errors:
                index = min(len(FakeRunner.calls) - 1, len(errors) - 1)
                outcome = errors[index]
                if outcome is not None:
                    raise outcome
                return result
            if error is not None:
                raise error
            return result

    class FakeOpenAIProvider:
        """Stands in for ``agents.OpenAIProvider`` -- ``_model_for`` only needs
        ``get_model`` to return something usable as ``Agent(model=...)``."""

        instances: list[FakeOpenAIProvider] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            FakeOpenAIProvider.instances.append(self)

        def get_model(self, model_name: str) -> str:
            return model_name

    agents_mod = types.ModuleType("agents")
    agents_mod.Agent = FakeAgent
    agents_mod.Runner = FakeRunner
    agents_mod.RunConfig = lambda **kw: types.SimpleNamespace(**kw)
    agents_mod.ModelSettings = lambda **kw: types.SimpleNamespace(**kw)
    agents_mod.OpenAIProvider = FakeOpenAIProvider
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
                "DSPy optimizer dependencies are not installed. Install with "
                "`pip install caliber-suite[dspy]` to enable DSPy refinement paths."
            )
        ),
    )

    candidate, _usage = provider.generate_candidate(
        _candidate_context(
            optimizer_type="DSPyBootstrapFewShot", trainset=[{"input": "Q", "expected": "A"}]
        )
    )

    assert candidate.content == "META-FALLBACK"
    assert "Install with `pip install caliber-suite[dspy]`" in candidate.rationale
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


# ---------------------------------------------------------------------------
# Retry behavior — see ``_is_retryable_llm_error`` / ``_run_agent_sync``.
# ---------------------------------------------------------------------------


def _openai_timeout_error() -> Exception:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APITimeoutError(request=request)


def _openai_status_error(status_code: int) -> Exception:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status_code, request=request)
    return openai.APIStatusError(f"http {status_code}", response=response, body=None)


# Tiny backoff bounds so these tests don't actually wait real seconds.
_NO_WAIT = {
    "llm_call_retry_base_delay_seconds": 0.001,
    "llm_call_retry_max_delay_seconds": 0.001,
}


def test_run_agent_sync_retries_a_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single transient timeout is absorbed -- the caller never sees it."""
    result = SimpleNamespace(final_output={"ok": True})
    _fake_agent, fake_runner = _install_agents_module(
        monkeypatch, errors=[_openai_timeout_error(), None], result=result
    )
    provider = _provider(**_NO_WAIT)

    out = provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")

    assert out is result
    assert len(fake_runner.calls) == 2


def test_run_agent_sync_retries_rate_limit_and_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 and 5xx are exactly as retryable as a raw timeout."""
    result = SimpleNamespace(final_output={"ok": True})
    _fake_agent, fake_runner = _install_agents_module(
        monkeypatch,
        errors=[_openai_status_error(429), _openai_status_error(503), None],
        result=result,
    )
    provider = _provider(llm_call_max_attempts=3, **_NO_WAIT)

    out = provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")

    assert out is result
    assert len(fake_runner.calls) == 3


def test_run_agent_sync_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistently failing provider still fails the job -- just not on the
    first blip, and not forever: exactly ``llm_call_max_attempts`` calls."""
    _fake_agent, fake_runner = _install_agents_module(
        monkeypatch,
        errors=[_openai_timeout_error()],  # repeats every call
    )
    provider = _provider(llm_call_max_attempts=3, **_NO_WAIT)

    with pytest.raises(LLMProviderError, match="diagnosis LLM call failed"):
        provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")

    assert len(fake_runner.calls) == 3


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_run_agent_sync_does_not_retry_non_transient_status_errors(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    """Auth/bad-request/not-found/validation errors fail on the first attempt
    -- retrying an identical malformed or unauthorized request cannot help,
    so spending the retry budget on it would only slow down a job that was
    always going to fail."""
    _fake_agent, fake_runner = _install_agents_module(
        monkeypatch, errors=[_openai_status_error(status_code)]
    )
    provider = _provider(llm_call_max_attempts=5, **_NO_WAIT)

    with pytest.raises(LLMProviderError, match="diagnosis LLM call failed"):
        provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")

    assert len(fake_runner.calls) == 1


def test_run_agent_sync_does_not_retry_a_plain_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only recognized OpenAI transport/provider errors are retried -- an
    arbitrary exception (a bug, a schema mismatch, ...) fails immediately."""
    _fake_agent, fake_runner = _install_agents_module(monkeypatch, errors=[RuntimeError("boom")])
    provider = _provider(llm_call_max_attempts=5, **_NO_WAIT)

    with pytest.raises(LLMProviderError, match="diagnosis LLM call failed: boom"):
        provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")

    assert len(fake_runner.calls) == 1


def test_run_agent_sync_defaults_to_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default retry budget (no explicit override) is 1 try + 2 retries."""
    _fake_agent, fake_runner = _install_agents_module(monkeypatch, errors=[_openai_timeout_error()])
    provider = _provider(**_NO_WAIT)

    with pytest.raises(LLMProviderError):
        provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1")

    assert len(fake_runner.calls) == 3


def test_model_for_applies_the_configured_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_ensure_diagnosis_agent`` resolves its model through a provider built
    with ``request_timeout_seconds`` -- not the SDK's own default -- and reuses
    it (one client, not one per agent)."""
    result = SimpleNamespace(final_output={"ok": True})
    _install_agents_module(monkeypatch, result=result)
    provider = _provider(request_timeout_seconds=7.5)

    provider._model_for("gpt-4o-mini")

    assert provider._model_provider is not None
    assert provider._model_provider.kwargs["openai_client"].timeout == 7.5
    # A second call reuses the same provider/client rather than rebuilding it.
    provider._model_for("gpt-4o-mini")
    assert len(type(provider._model_provider).instances) == 1


def test_run_agent_sync_falls_back_when_runner_does_not_accept_run_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(final_output={"ok": True})

    class LegacyRunner:
        @staticmethod
        def run_sync(agent: object, prompt: str) -> object:
            return result

    agents_mod = types.ModuleType("agents")
    agents_mod.Runner = LegacyRunner
    agents_mod.RunConfig = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "agents", agents_mod)

    provider = _provider()

    assert provider._run_agent_sync(object(), "prompt", stage="diagnosis", item_id="FB-1") is result


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
        def __init__(self, *, model: str, inference_params: object = None) -> None:
            captured["correctness_model"] = model
            captured["correctness_inference_params"] = inference_params

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

        def _create(
            self, *, model: str, messages: list[dict[str, str]], **kwargs: object
        ) -> object:
            captured["openai_request"] = {"model": model, "messages": messages, **kwargs}
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
