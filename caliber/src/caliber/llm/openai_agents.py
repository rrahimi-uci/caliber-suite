"""Production LLM provider backed by the OpenAI Agents SDK.

Wraps ``Agent`` + ``Runner.run_sync`` from the ``openai-agents`` package
(``[llm]`` install extra). The provider is lazy-imported through the
factory so the rest of CALIBER stays importable in tests and dev
environments that don't have the SDK installed.

The agent definitions here map one agent per orchestrator stage that needs LLM work:
one agent per orchestrator stage that needs LLM work. Currently only
:meth:`OpenAIAgentsLLMProvider.diagnose` is wired up.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from caliber.extensibility import OptimizerSpec, PluginError, optimizer_registry
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    EvidenceContext,
    LLMProviderError,
    LLMUsage,
    PromptCandidate,
    TriageClassification,
    TriageContext,
    WorkflowEdit,
    WorkflowEditContext,
    WorkflowGenerationContext,
)
from caliber.runtime_advisories import dspy_optimizer_block_reason

if TYPE_CHECKING:
    # Typing-only imports — never executed, so the SDK is not required for
    # mypy unless ``[llm]`` is installed.
    pass

logger = logging.getLogger("caliber.llm.openai_agents")

_DSPY_OPTIMIZER = Callable[..., tuple[PromptCandidate, LLMUsage]]


_DIAGNOSIS_AGENT_INSTRUCTIONS = """\
You are CALIBER's root-cause analyst.

Given evidence about a flagged AI-agent failure, produce a structured
diagnosis with these properties:

* root_cause: a single sentence naming the root cause. Be specific —
  "the prompt does not require the agent to call lookup_policy" is good;
  "the prompt is bad" is not.
* affected_components: list the components that need to change to fix
  the root cause. Valid values: "prompt", "skill",
  "handoff_schema", "guardrail", "rag_config".
* confidence: how confident you are in the root_cause, on a 0-1 scale.
  Use 0.6 or below if multiple causes are plausible.
* alternatives: up to three competing hypotheses you considered and
  rejected. Empty list when the root cause is unambiguous.

Be precise. The next stage uses your diagnosis to pick an optimizer.
"""


_TRIAGE_AGENT_INSTRUCTIONS = """\
You are CALIBER's intake triage classifier.

Given a freshly-verified feedback item about an AI agent, classify it so the
refinement pipeline can route it. Produce a structured TriageClassification:

* cluster: a short failure-mode label, e.g. "tool_use", "hallucination",
  "context_drift", "formatting", "refusal", "other".
* artifact_type: which artifact most likely needs to change. One of
  "prompt" or "skill". Prefer "skill" for tool-use / tool-calling failures
  ONLY when the item indicates the agent has skills available
  (``agent_has_skills`` is true) — otherwise "prompt" (including multi-turn /
  memory / context-drift failures).
* confidence: 0-1. Use 0.5 or below when the category is ambiguous.
* rationale: one sentence tying the classification to the item's free_text.

Read the free_text carefully — the stated category is a hint, not ground truth.
"""


_METAPROMPT_CANDIDATE_INSTRUCTIONS = """\
You are CALIBER's MetaPrompt candidate generator.

You will receive:
* the current artifact (typically a system prompt),
* a structured diagnosis pinpointing why the agent failed,
* the artifact type.

Produce a structured PromptCandidate with these properties:

* artifact_type: echo back the input artifact_type unchanged.
* content: the new artifact content. It must be a minimal edit of the
  current content — preserve every directive that wasn't implicated in
  the diagnosis. Do not rewrite the whole prompt unless the diagnosis
  explicitly calls for it.
* rationale: one or two sentences explaining how your edit addresses
  the diagnosis.
* diff_summary: a short summary like "+5 / -3 lines" so a reviewer
  can size up the change at a glance.

When the current artifact is empty (cold-start), generate a minimal
starting prompt that addresses the diagnosis directly.
"""


_WORKFLOW_EDIT_AGENT_INSTRUCTIONS = """\
You are CALIBER's in-canvas workflow copilot.

You will receive:
* the current workflow manifest (full JSON),
* a natural-language instruction describing the change to make,
* a grounding bundle listing the registry artifacts that exist
  (tools, skills, eval datasets).

Produce a structured result with these properties:

* manifest_json: the COMPLETE edited workflow manifest, as a JSON string.
  Apply the instruction as a *minimal* edit — preserve every node, edge,
  and field the instruction does not concern. Keep the same schema_version
  and workflow_id. The result must be a valid CALIBER workflow manifest.
* summary: one line describing the change (e.g. "Add a PII-redact guardrail
  after the rules agent").
* rationale: one or two sentences on why the edit satisfies the instruction.

Rules:
* Only reference tools, skills, and eval datasets that appear in the
  grounding bundle — never invent registry refs.
* If the instruction is unclear or cannot be applied safely, return the
  manifest unchanged and explain why in the rationale.
"""


_WORKFLOW_GEN_AGENT_INSTRUCTIONS = """\
You are CALIBER's plan-to-build workflow author.

You will receive:
* a plain-language goal describing what the workflow should do,
* a base manifest skeleton (its identity fields only — workflow_id,
  schema_version, name — and whatever nodes already exist on the canvas),
* a grounding bundle listing the registry artifacts that exist
  (tools, skills, eval datasets).

Produce a structured result with these properties:

* manifest_json: the COMPLETE workflow manifest authored toward the goal,
  as a JSON string. Author the full graph — nodes and edges — needed to
  satisfy the goal. Keep the skeleton's schema_version and workflow_id. Wire
  a sensible start → … → output flow. The result must be a valid CALIBER
  workflow manifest.
* summary: one line describing the workflow you built (e.g. "3-step support
  triage: classify → answer → guardrail").
* rationale: one or two sentences on how the graph satisfies the goal.

Rules:
* Only reference tools, skills, and eval datasets that appear in the
  grounding bundle — never invent registry refs.
* Prefer the smallest graph that genuinely satisfies the goal.
* If the goal is too vague to author safely, return the base manifest
  unchanged and explain what's missing in the rationale.
"""


class _WorkflowEditDraft(BaseModel):
    """Agent-facing output: the manifest as a JSON *string*.

    The manifest is open-shaped, which is awkward to express as a strict
    structured-output schema. Asking the model for a JSON string and parsing
    it here is far more reliable than a deeply-nested object schema; the route
    re-validates it against the manifest model regardless.
    """

    model_config = ConfigDict(frozen=True)

    manifest_json: str = Field(min_length=1)
    summary: str = Field(default="")
    rationale: str = Field(default="")


class OpenAIAgentsLLMProvider:
    """Production LLM provider.

    Parameters
    ----------
    api_key:
        The provider API key. Pulled from the env var named by
        ``CaliberConfig.llm_api_key_env`` at construction time (see
        :func:`caliber.llm.provider.build_provider`); never stored on the
        provider object beyond ``__init__``-time use to configure the SDK.
    diagnosis_model:
        Model identifier (e.g. ``gpt-4o-mini``) passed to the diagnosis
        agent.
    """

    def __init__(
        self,
        api_key: str,
        diagnosis_model: str,
        gepa_reflection_model: str | None = None,
        gepa_max_metric_calls: int = 100,
        dspy_max_bootstrapped_demos: int = 4,
        dspy_max_labeled_demos: int = 4,
        dspy_mipro_auto: str = "light",
        allow_flagged_dspy_optimizers: bool = False,
    ) -> None:
        # The OpenAI Agents SDK reads the key from the ``OPENAI_API_KEY``
        # env var or from the OpenAI client we configure. We don't keep
        # ``api_key`` on ``self`` so an accidental log of the provider
        # object can't leak the secret.
        self._diagnosis_model = diagnosis_model
        # The diagnosis and MetaPrompt/skill/DSPy paths reuse this model knob.
        # GEPA has the separate reflection-model setting below.
        self._candidate_model = diagnosis_model
        self._gepa_reflection_model = _normalize_reflection_model(
            gepa_reflection_model or diagnosis_model
        )
        self._gepa_max_metric_calls = gepa_max_metric_calls
        self._dspy_max_bootstrapped_demos = dspy_max_bootstrapped_demos
        self._dspy_max_labeled_demos = dspy_max_labeled_demos
        self._dspy_mipro_auto = dspy_mipro_auto
        self._allow_flagged_dspy_optimizers = allow_flagged_dspy_optimizers
        self._diagnosis_agent: Any | None = None
        self._candidate_agent: Any | None = None
        self._workflow_edit_agent: Any | None = None
        self._workflow_gen_agent: Any | None = None
        self._triage_agent: Any | None = None
        self._set_api_key(api_key)

    @staticmethod
    def _set_api_key(api_key: str) -> None:
        """Push the API key into the OpenAI client environment.

        Done as a side effect rather than holding the value on the instance
        so it goes through OpenAI's own ``api_key`` plumbing — which is the
        path the OpenAI Agents SDK reads from.

        Uses unconditional assignment (not ``setdefault``) so the key
        resolved from :attr:`CaliberConfig.llm_api_key_env` always wins
        over a stray ``OPENAI_API_KEY`` inherited from the process
        environment. Without this, a deployment that explicitly
        configures its key via ``file://`` or a non-default env-var
        name would silently get whatever was in ``OPENAI_API_KEY``
        instead — a confusing environment-dependent behavior we hit
        in the V2 review (Finding 7).
        """
        import os  # noqa: PLC0415  -- local for the same reason as the agents import

        os.environ["OPENAI_API_KEY"] = api_key
        # The OpenAI Agents SDK enables hosted tracing by default. Disable it
        # unless the deployment explicitly opted in via environment.
        os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

    def _agent_class(self) -> Any:
        """Return the ``agents.Agent`` class, raising :class:`LLMProviderError`
        with a clear message when the ``[llm]`` extra isn't installed."""
        try:
            from agents import Agent  # noqa: PLC0415  -- deliberate lazy import
        except ImportError as exc:
            raise LLMProviderError(
                "openai-agents is not installed. Install with "
                "`pip install caliber-suite[llm]` to enable LLM providers."
            ) from exc
        return Agent

    def _ensure_triage_agent(self) -> Any:
        if self._triage_agent is not None:
            return self._triage_agent
        agent_cls = self._agent_class()
        self._triage_agent = agent_cls(
            name="caliber.triage",
            instructions=_TRIAGE_AGENT_INSTRUCTIONS,
            output_type=TriageClassification,
            model=self._diagnosis_model,
        )
        return self._triage_agent

    def _ensure_diagnosis_agent(self) -> Any:
        if self._diagnosis_agent is not None:
            return self._diagnosis_agent
        agent_cls = self._agent_class()
        self._diagnosis_agent = agent_cls(
            name="caliber.diagnosis",
            instructions=_DIAGNOSIS_AGENT_INSTRUCTIONS,
            output_type=Diagnosis,
            model=self._diagnosis_model,
        )
        return self._diagnosis_agent

    def _ensure_candidate_agent(self) -> Any:
        """Lazy-construct the MetaPrompt Agent used by two paths and fallbacks.

        :meth:`generate_candidate` dispatches GEPA and DSPy before reaching this
        helper. MetaPrompt and SkillMetaPrompt share this OpenAI Agent, and the
        optional optimizer paths also use it for their documented fallback.
        """
        if self._candidate_agent is not None:
            return self._candidate_agent
        agent_cls = self._agent_class()
        self._candidate_agent = agent_cls(
            name="caliber.candidate.metaprompt",
            instructions=_METAPROMPT_CANDIDATE_INSTRUCTIONS,
            output_type=PromptCandidate,
            model=self._candidate_model,
        )
        return self._candidate_agent

    def _ensure_workflow_edit_agent(self) -> Any:
        if self._workflow_edit_agent is not None:
            return self._workflow_edit_agent
        agent_cls = self._agent_class()
        self._workflow_edit_agent = agent_cls(
            name="caliber.workflow.copilot",
            instructions=_WORKFLOW_EDIT_AGENT_INSTRUCTIONS,
            output_type=_WorkflowEditDraft,
            model=self._candidate_model,
        )
        return self._workflow_edit_agent

    def _ensure_workflow_gen_agent(self) -> Any:
        if self._workflow_gen_agent is not None:
            return self._workflow_gen_agent
        agent_cls = self._agent_class()
        self._workflow_gen_agent = agent_cls(
            name="caliber.workflow.plan_build",
            instructions=_WORKFLOW_GEN_AGENT_INSTRUCTIONS,
            output_type=_WorkflowEditDraft,
            model=self._candidate_model,
        )
        return self._workflow_gen_agent

    def classify_triage(self, context: TriageContext) -> tuple[TriageClassification, LLMUsage]:
        """Run the triage Agent on the item and return its structured classification."""
        agent = self._ensure_triage_agent()
        prompt = _format_triage_prompt(context)
        result = self._run_agent_sync(agent, prompt, stage="triage", item_id=context.item_id)
        classification = _extract_output(result, TriageClassification, stage="triage")
        usage = _extract_usage(result)
        return classification, usage

    def diagnose(self, evidence: EvidenceContext) -> tuple[Diagnosis, LLMUsage]:
        """Run the diagnosis Agent on the evidence and return its structured output."""
        agent = self._ensure_diagnosis_agent()
        prompt = _format_evidence_prompt(evidence)
        result = self._run_agent_sync(agent, prompt, stage="diagnosis", item_id=evidence.item_id)
        diagnosis = _extract_output(result, Diagnosis, stage="diagnosis")
        usage = _extract_usage(result)
        return diagnosis, usage

    def generate_candidate(self, context: CandidateContext) -> tuple[PromptCandidate, LLMUsage]:
        """Run the candidate Agent and return the structured PromptCandidate.

        Which optimizers exist, and what each may target, comes from
        :mod:`caliber.extensibility.registry` rather than from a chain of string
        comparisons here. The registry answers "is this a real name" and "may it
        target this artifact kind"; this method only knows how to run the
        built-in engines:

        * ``"MetaPrompt"`` / ``"SkillMetaPrompt"`` — single-pass rewrite
          via the OpenAI Agents SDK.
        * ``"GEPA"`` — multi-generation genetic-Pareto optimization via
          MLflow's ``GepaPromptOptimizer``. Falls back to MetaPrompt if
          the ``gepa`` library is not installed.
        * ``"DSPyBootstrapFewShot"`` / ``"DSPyMIPRO"`` — DSPy teleprompters
          (few-shot demo selection; MIPRO also rewrites the instruction).
          Fall back to MetaPrompt if the ``[dspy]`` extra is not installed or
          the trainset is empty.

        An unregistered name raises :class:`LLMProviderError` naming what *is*
        available, so a config mistake reads as one.
        """
        spec = self._resolve_optimizer(context)

        if spec.name == "GEPA":
            return self._generate_candidate_gepa(context)

        if spec.name in ("DSPyBootstrapFewShot", "DSPyMIPRO"):
            return self._generate_candidate_dspy(context)

        if spec.name in ("MetaPrompt", "SkillMetaPrompt"):
            return self._generate_candidate_metaprompt(context)

        # Registered, and this provider has no engine for it. Reachable only via
        # a plugin, whose own execution path lands in a later milestone; until
        # then saying so beats running MetaPrompt under the plugin's name and
        # attributing the result to code that never ran.
        raise LLMProviderError(
            f"optimizer {spec.name!r} is registered by "
            f"{spec.distribution or 'an unknown distribution'} but this provider "
            "has no engine for it"
        )

    def _resolve_optimizer(self, context: CandidateContext) -> OptimizerSpec:
        """Look the optimizer up and check it against the job's artifact kind.

        The artifact check is the part the old dispatch chain could not do. A
        skill job routed to ``MetaPrompt`` used to run: it rewrote the content
        with the prompt formatter and ignored ``allowed_tools`` entirely, so a
        skill could come back with its tool restrictions dropped and nothing in
        the result would say so. A mismatch is a configuration error and now
        reads as one.
        """
        try:
            spec = optimizer_registry().get(context.optimizer_type)
        except PluginError as exc:
            raise LLMProviderError(str(exc)) from exc

        if not spec.can_target(context.artifact_type):
            raise LLMProviderError(
                f"optimizer {spec.name!r} cannot target artifact_type "
                f"{context.artifact_type!r} (it targets "
                f"{sorted(spec.artifact_types)}); "
                f"available for {context.artifact_type!r}: "
                f"{optimizer_registry().names(artifact_type=context.artifact_type)}"
            )
        return spec

    def _generate_candidate_metaprompt(
        self, context: CandidateContext
    ) -> tuple[PromptCandidate, LLMUsage]:
        """Single-pass MetaPrompt rewrite via the OpenAI Agents SDK.

        Shared by the ``MetaPrompt``/``SkillMetaPrompt`` paths and used as the
        graceful fallback when an optional optimizer dependency (gepa, dspy) is
        not installed.
        """
        agent = self._ensure_candidate_agent()
        prompt = _format_candidate_prompt(context)
        result = self._run_agent_sync(agent, prompt, stage="candidate", item_id=context.job_id)
        candidate = _extract_output(result, PromptCandidate, stage="candidate")
        usage = _extract_usage(result)
        return candidate, usage

    def propose_workflow_edit(self, context: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        """Run the copilot agent and return the proposed full manifest.

        The agent emits the manifest as a JSON string (open-shaped manifests
        don't strict-schema well); we parse it here and raise
        :class:`LLMProviderError` if the model returned non-JSON. The route
        re-validates the parsed manifest against the manifest model.
        """
        agent = self._ensure_workflow_edit_agent()
        prompt = _format_workflow_edit_prompt(context)
        result = self._run_agent_sync(agent, prompt, stage="workflow_edit", item_id="copilot")
        draft = _extract_output(result, _WorkflowEditDraft, stage="workflow_edit")
        try:
            manifest = json.loads(draft.manifest_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMProviderError(
                f"workflow_edit agent returned non-JSON manifest: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise LLMProviderError(
                "workflow_edit agent returned a manifest that is not a JSON object"
            )
        edit = WorkflowEdit(manifest=manifest, summary=draft.summary, rationale=draft.rationale)
        usage = _extract_usage(result)
        return edit, usage

    def generate_workflow_from_goal(
        self, context: WorkflowGenerationContext
    ) -> tuple[WorkflowEdit, LLMUsage]:
        """Run the plan-to-build agent and return the authored full manifest.

        Blank-slate sibling of :meth:`propose_workflow_edit` — same JSON-string
        manifest contract (open-shaped manifests don't strict-schema well), so
        we parse here and raise :class:`LLMProviderError` on non-JSON; the route
        re-validates the parsed manifest against the manifest model.
        """
        agent = self._ensure_workflow_gen_agent()
        prompt = _format_workflow_gen_prompt(context)
        result = self._run_agent_sync(agent, prompt, stage="workflow_gen", item_id="plan_build")
        draft = _extract_output(result, _WorkflowEditDraft, stage="workflow_gen")
        try:
            manifest = json.loads(draft.manifest_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMProviderError(f"workflow_gen agent returned non-JSON manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise LLMProviderError(
                "workflow_gen agent returned a manifest that is not a JSON object"
            )
        edit = WorkflowEdit(manifest=manifest, summary=draft.summary, rationale=draft.rationale)
        usage = _extract_usage(result)
        return edit, usage

    def _generate_candidate_gepa(
        self, context: CandidateContext
    ) -> tuple[PromptCandidate, LLMUsage]:
        """Use MLflow's GepaPromptOptimizer for candidate generation.

        GEPA (Genetic-Pareto) evolves a population of prompt variants
        across multiple generations using reflective mutation and
        Pareto-aware selection. Best suited when the diagnosis has
        low confidence, many alternatives, or competing objectives.

        The method wraps the current artifact content as a registered
        MLflow prompt, runs GEPA optimization, and returns the best
        candidate.
        """
        try:
            from mlflow.genai.optimize.optimizers import GepaPromptOptimizer  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "gepa library not installed; falling back to MetaPrompt for job=%s",
                context.job_id,
            )
            # Graceful fallback — run MetaPrompt instead.
            return self._generate_candidate_metaprompt(context)

        try:
            import mlflow  # noqa: PLC0415
            from mlflow.genai.scorers import Correctness  # noqa: PLC0415
        except ImportError as exc:
            raise LLMProviderError(
                "mlflow[genai] is required for GEPA optimization. "
                "Install with `pip install mlflow[genai] gepa`."
            ) from exc

        current_content = context.current_artifact_content or ""
        artifact_label = context.skill_name or context.agent_id

        try:
            # Register a temporary prompt for GEPA to optimize.
            prompt_name = f"caliber-gepa-{context.job_id}"
            prompt_version = mlflow.genai.register_prompt(
                name=prompt_name,
                template=current_content if current_content else f"You are {artifact_label}.",
                commit_message=f"GEPA baseline for job {context.job_id}",
            )

            # Build the predict_fn that GEPA will optimize.
            import openai as _openai  # noqa: PLC0415

            def _predict_fn(**kwargs: object) -> str:
                p = mlflow.genai.load_prompt(prompt_name)
                client = _openai.OpenAI()
                resp = client.chat.completions.create(
                    model=self._candidate_model,
                    messages=[{"role": "system", "content": p.format(**kwargs)}],
                )
                return resp.choices[0].message.content or ""

            # Construct the optimizer with config-driven params.
            reflection_model = self._gepa_reflection_model
            optimizer = GepaPromptOptimizer(
                reflection_model=reflection_model,
                max_metric_calls=self._gepa_max_metric_calls,
                display_progress_bar=False,
            )

            # Run optimization — the dataset is minimal since CALIBER's
            # eval gate is the real quality check. GEPA explores the
            # prompt space; the eval stage validates the winner.
            dataset = [
                {
                    "inputs": {"task": context.diagnosis.root_cause},
                    "outputs": f"Address: {context.diagnosis.root_cause}",
                },
            ]

            result = mlflow.genai.optimize_prompts(
                predict_fn=_predict_fn,
                train_data=dataset,
                prompt_uris=[prompt_version.uri],
                optimizer=optimizer,
                scorers=[Correctness(model=reflection_model)],
                enable_tracking=True,
            )

            optimized_raw = result.optimized_prompts[0].template
            optimized_content = (
                optimized_raw if isinstance(optimized_raw, str) else json.dumps(optimized_raw)
            )
            candidate = PromptCandidate(
                artifact_type=context.artifact_type,
                content=optimized_content,
                rationale=(
                    f"GEPA optimization (score: {result.initial_eval_score} → "
                    f"{result.final_eval_score}): {context.diagnosis.root_cause}"
                ),
                diff_summary="GEPA multi-generation evolution",
            )
            usage = LLMUsage(
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
            )
            return candidate, usage

        except Exception as exc:
            logger.exception("GEPA optimization failed for job=%s", context.job_id)
            raise LLMProviderError(f"GEPA optimization failed: {exc}") from exc

    def _generate_candidate_dspy(
        self, context: CandidateContext
    ) -> tuple[PromptCandidate, LLMUsage]:
        """Run a DSPy teleprompter (BootstrapFewShot or MIPROv2) for candidate gen.

        Both wrap the current prompt as a ``question -> answer`` DSPy program and
        optimize it against the agent's eval dataset: BootstrapFewShot selects
        few-shot demonstrations; MIPROv2 *additionally* rewrites the instruction.
        See :mod:`caliber.llm.dspy_optimizer` for the bridge details.

        Falls back to MetaPrompt when the trainset is empty — a DSPy
        teleprompter has nothing to optimize against. Any other failure surfaces
        as :class:`LLMProviderError`, mirroring the GEPA handler.
        """
        optimizer = context.optimizer_type
        try:
            empty_trainset_error, run_bootstrap_fewshot, run_mipro = _load_dspy_optimizer_bridge()
        except LLMProviderError as exc:
            logger.warning("%s unavailable for job=%s: %s", optimizer, context.job_id, exc)
            candidate, usage = self._generate_candidate_metaprompt(context)
            return (
                _annotate_optimizer_fallback(
                    candidate,
                    optimizer=optimizer,
                    reason=str(exc),
                ),
                usage,
            )
        runtime_block_reason = dspy_optimizer_block_reason(
            allow_flagged=self._allow_flagged_dspy_optimizers
        )
        if runtime_block_reason is not None:
            logger.warning(
                "%s disabled for job=%s: %s",
                optimizer,
                context.job_id,
                runtime_block_reason,
            )
            candidate, usage = self._generate_candidate_metaprompt(context)
            return (
                _annotate_optimizer_fallback(
                    candidate,
                    optimizer=optimizer,
                    reason=runtime_block_reason,
                ),
                usage,
            )
        if not context.trainset:
            logger.warning(
                "%s: no trainset for job=%s; falling back to MetaPrompt",
                optimizer,
                context.job_id,
            )
            return self._generate_candidate_metaprompt(context)

        max_bootstrapped = (
            context.dspy_max_bootstrapped_demos
            if context.dspy_max_bootstrapped_demos is not None
            else self._dspy_max_bootstrapped_demos
        )
        max_labeled = (
            context.dspy_max_labeled_demos
            if context.dspy_max_labeled_demos is not None
            else self._dspy_max_labeled_demos
        )

        try:
            if optimizer == "DSPyMIPRO":
                return run_mipro(
                    context=context,
                    model=self._candidate_model,
                    max_bootstrapped_demos=max_bootstrapped,
                    max_labeled_demos=max_labeled,
                    auto=context.dspy_mipro_auto or self._dspy_mipro_auto,
                )
            return run_bootstrap_fewshot(
                context=context,
                model=self._candidate_model,
                max_bootstrapped_demos=max_bootstrapped,
                max_labeled_demos=max_labeled,
            )
        except empty_trainset_error:
            logger.warning(
                "%s: trainset had no usable rows for job=%s; falling back to MetaPrompt",
                optimizer,
                context.job_id,
            )
            return self._generate_candidate_metaprompt(context)
        except Exception as exc:
            logger.exception("DSPy %s failed for job=%s", optimizer, context.job_id)
            raise LLMProviderError(f"DSPy {optimizer} failed: {exc}") from exc

    def _run_agent_sync(self, agent: Any, prompt: str, *, stage: str, item_id: str) -> Any:
        """Invoke ``Runner.run_sync`` with uniform error handling.

        Centralizing the SDK call means every stage produces the same
        :class:`LLMProviderError` shape on failure (auth, rate limit,
        transport) so the worker's exception handling stays simple.
        """
        try:
            from agents import RunConfig, Runner  # noqa: PLC0415
        except ImportError as exc:
            raise LLMProviderError(
                "openai-agents is not installed. Install with "
                "`pip install caliber-suite[llm]` to enable LLM providers."
            ) from exc

        try:
            run_config = RunConfig(tracing_disabled=True)
            params: Iterable[inspect.Parameter]
            try:
                params = inspect.signature(Runner.run_sync).parameters.values()
            except (TypeError, ValueError):
                params = ()
            if any(
                param.name == "run_config" or param.kind is inspect.Parameter.VAR_KEYWORD
                for param in params
            ):
                return Runner.run_sync(agent, prompt, run_config=run_config)
            return Runner.run_sync(agent, prompt)
        except Exception as exc:
            logger.exception("%s agent failed for id=%s", stage, item_id)
            raise LLMProviderError(f"{stage} LLM call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers (kept module-private; not part of the public surface)
# ---------------------------------------------------------------------------


def _format_triage_prompt(context: TriageContext) -> str:
    """Render :class:`TriageContext` into the message the triage Agent receives."""
    payload = {
        "agent_id": context.agent_id,
        "item_id": context.item_id,
        "category": context.category,
        "severity": context.severity,
        "free_text": context.free_text,
        "agent_has_skills": context.agent_has_skills,
    }
    return (
        "Classify the following CALIBER verification item. "
        "Respond with the structured TriageClassification schema.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )


def _format_evidence_prompt(evidence: EvidenceContext) -> str:
    """Render :class:`EvidenceContext` into the message the Agent receives.

    Stable JSON-ish format so the agent's behavior is reproducible — a
    free-text prompt would drift every time we touched the orchestrator.
    """
    payload = {
        "agent_id": evidence.agent_id,
        "item_id": evidence.item_id,
        "category": evidence.category,
        "severity": evidence.severity,
        "free_text": evidence.free_text,
        "trace_id": evidence.trace_id,
        "session_id": evidence.session_id,
        "evidence_summary": evidence.evidence_summary,
    }
    return (
        "Diagnose the following CALIBER verification item. "
        "Respond with the structured Diagnosis schema.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )


def _normalize_reflection_model(model: str) -> str:
    """Return the MLflow/OpenAI reflection model URI GEPA expects."""
    cleaned = model.strip()
    if cleaned.startswith("openai:/"):
        return cleaned
    return f"openai:/{cleaned}"


def _load_dspy_optimizer_bridge() -> tuple[type[Exception], _DSPY_OPTIMIZER, _DSPY_OPTIMIZER]:
    """Load the optional DSPy bridge only when a DSPy optimizer is selected."""

    try:
        from caliber.llm.dspy_optimizer import (  # noqa: PLC0415
            EmptyTrainsetError,
            run_bootstrap_fewshot,
            run_mipro,
        )
    except ImportError as exc:
        raise LLMProviderError(
            "DSPy optimizer dependencies are not installed. Install with "
            "`pip install caliber-suite[dspy]` to enable DSPy refinement paths."
        ) from exc
    return EmptyTrainsetError, run_bootstrap_fewshot, run_mipro


def _format_candidate_prompt(context: CandidateContext) -> str:
    """Render :class:`CandidateContext` into the message the Agent receives.

    The current artifact is included verbatim inside a fenced block so the
    Agent can produce a minimal edit. Diagnosis is rendered as JSON for
    deterministic parsing by the model. When ``review_notes`` is set
    (retry path), it appears as a final "reviewer guidance" block the
    model is instructed to address.
    """
    diagnosis_dump = {
        "root_cause": context.diagnosis.root_cause,
        "affected_components": list(context.diagnosis.affected_components),
        "confidence": context.diagnosis.confidence,
        "alternatives": list(context.diagnosis.alternatives),
    }
    current = context.current_artifact_content or "(no current artifact — cold start)"
    base = (
        f"Generate a PromptCandidate using the {context.optimizer_type} pattern.\n\n"
        f"Agent: {context.agent_id}\n"
        f"Artifact type: {context.artifact_type}\n\n"
        f"Diagnosis (JSON):\n```json\n{json.dumps(diagnosis_dump, indent=2)}\n```\n\n"
        f"Current artifact:\n```\n{current}\n```\n"
    )
    if context.review_notes:
        base += (
            f"\nReviewer guidance (from a prior approval that requested changes — "
            f"the new candidate must address this):\n```\n{context.review_notes}\n```\n"
        )
    return base


def _annotate_optimizer_fallback(
    candidate: PromptCandidate,
    *,
    optimizer: str,
    reason: str,
) -> PromptCandidate:
    """Annotate a MetaPrompt result when a higher-tier optimizer was skipped."""

    note = f"{optimizer} fallback: {reason} Using MetaPrompt instead."
    rationale = f"{candidate.rationale} {note}".strip() if candidate.rationale else note
    diff_summary = candidate.diff_summary.strip()
    fallback_summary = f"{optimizer} -> MetaPrompt fallback"
    diff_summary = f"{diff_summary}; {fallback_summary}" if diff_summary else fallback_summary
    return PromptCandidate(
        artifact_type=candidate.artifact_type,
        content=candidate.content,
        rationale=rationale,
        diff_summary=diff_summary,
    )


def _format_workflow_edit_prompt(context: WorkflowEditContext) -> str:
    """Render :class:`WorkflowEditContext` into the copilot agent's message.

    The current manifest and grounding bundle are rendered as JSON so the
    agent has a deterministic, machine-readable view of what exists. The
    instruction is the user's verbatim natural-language request.
    """
    return (
        "Edit the following CALIBER workflow manifest per the instruction. "
        "Return the COMPLETE edited manifest as manifest_json.\n\n"
        f"Instruction:\n{context.instruction}\n\n"
        f"Current manifest (JSON):\n```json\n"
        f"{json.dumps(context.manifest, indent=2, default=str)}\n```\n\n"
        f"Available registry artifacts (only reference these):\n```json\n"
        f"{json.dumps(context.grounding, indent=2, default=str)}\n```\n"
    )


def _format_workflow_gen_prompt(context: WorkflowGenerationContext) -> str:
    """Render :class:`WorkflowGenerationContext` into the plan-build agent's message.

    The base manifest (identity + any existing nodes) and grounding bundle are
    rendered as JSON for a deterministic view of what exists; the goal is the
    user's verbatim plain-language request for what to build.
    """
    return (
        "Author a CALIBER workflow manifest that satisfies the goal. "
        "Return the COMPLETE manifest as manifest_json.\n\n"
        f"Goal:\n{context.goal}\n\n"
        f"Base manifest skeleton (keep its workflow_id + schema_version):\n```json\n"
        f"{json.dumps(context.manifest, indent=2, default=str)}\n```\n\n"
        f"Available registry artifacts (only reference these):\n```json\n"
        f"{json.dumps(context.grounding, indent=2, default=str)}\n```\n"
    )


_StructuredOutput = Diagnosis | PromptCandidate | TriageClassification | _WorkflowEditDraft


def _extract_output(
    result: Any,
    expected_type: type[_StructuredOutput],
    *,
    stage: str,
) -> Any:
    """Pull the structured output out of an OpenAI Agents SDK result.

    The SDK returns an object with a ``final_output`` attribute that, when
    ``output_type=X`` was set on the agent, holds the parsed model instance
    directly. We validate defensively because the SDK's behavior can shift
    across minor versions.
    """
    final = getattr(result, "final_output", None)
    if isinstance(final, expected_type):
        return final
    if isinstance(final, dict):
        return expected_type.model_validate(final)
    raise LLMProviderError(f"{stage} agent returned unexpected output type: {type(final).__name__}")


def _extract_usage(result: Any) -> LLMUsage:
    """Extract token counts from the result, with sensible fallbacks.

    The OpenAI Agents SDK surfaces usage on the run result object. Field
    names can vary across versions, so we probe a few common shapes rather
    than depend on a single one.
    """
    raw = getattr(result, "usage", None) or getattr(result, "token_usage", None)
    if raw is None:
        return LLMUsage()
    # Coerce defensively: usage telemetry is a side-channel and must NEVER turn
    # a successful LLM call into a failure. A non-numeric token/cost field (the
    # SDK's shapes vary across versions) previously raised a bare ValueError —
    # not wrapped as LLMProviderError — which escaped the provider contract and
    # could even wedge the circuit breaker on a HALF_OPEN probe.
    input_tokens = _safe_int(_get(raw, "input_tokens", "prompt_tokens"))
    output_tokens = _safe_int(_get(raw, "output_tokens", "completion_tokens"))
    cost = _safe_float(_get(raw, "cost", "cost_usd"))
    return LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)


def _safe_int(value: Any) -> int:
    """Best-effort int coercion; returns 0 on any non-numeric value."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    """Best-effort float coercion; returns 0.0 on any non-numeric value."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _get(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None
