"""LLM provider Protocol + shared types.

The Protocol is intentionally narrow: it exposes one operation per orchestrator
stage that needs an LLM. Today there's one — :meth:`LLMProvider.diagnose`.
Phase 3 adds :meth:`generate_candidate`; later stages add their own.

The factory function :func:`build_provider` is the single seam where
``CaliberConfig.llm_provider`` is mapped to an implementation. Adding a new
provider means adding one branch here and exposing it via ``__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from caliber.config import CaliberConfig

# ---------------------------------------------------------------------------
# Shared types: structured outputs and usage telemetry
# ---------------------------------------------------------------------------


class Diagnosis(BaseModel):
    """Structured output of the diagnosis stage.

    Matches the schema: root cause,
    affected components, confidence (0..1), and a list of alternative
    hypotheses the diagnoser considered.

    A Pydantic model rather than a dataclass because the OpenAI Agents SDK
    consumes Pydantic models as its ``output_type`` parameter for structured
    LLM responses.
    """

    model_config = ConfigDict(frozen=True)

    root_cause: str = Field(min_length=1)
    affected_components: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    alternatives: list[str] = Field(default_factory=list)


class PromptCandidate(BaseModel):
    """Structured output of the candidate-generation stage.

    Matches the schema: the
    rewritten artifact content plus a short rationale connecting the
    change to the upstream diagnosis. The schema is intentionally
    minimal — diff visualizations are derived from ``content`` and the
    current artifact in the UI rather than serialized here.
    """

    model_config = ConfigDict(frozen=True)

    artifact_type: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1)
    rationale: str = Field(default="")
    diff_summary: str = Field(default="")


class WorkflowEdit(BaseModel):
    """Structured output of the in-canvas copilot's edit stage.

    **Modify-in-place:** ``manifest`` is the *full* proposed workflow manifest
    — the model returns the entire edited document, not a patch — so the route
    can diff it against the base and the UI can render an accept/reject overlay.
    ``summary`` is a one-line description for the diff header; ``rationale``
    explains the change.
    """

    model_config = ConfigDict(frozen=True)

    manifest: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(default="")
    rationale: str = Field(default="")


class TriageClassification(BaseModel):
    """Structured output of the triage classifier.

    Triage turns a freshly-verified feedback item into a routing decision:
    which failure ``cluster`` it belongs to, which ``artifact_type`` most likely
    needs to change (``prompt`` / ``skill``), how
    confident the classifier is, and a short ``rationale`` recorded on the audit
    row. The deterministic skill *resolution* (which named skill) still happens
    in the triage stage, so the model never has to invent a skill name.
    """

    model_config = ConfigDict(frozen=True)

    cluster: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1, max_length=32)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="")


@dataclass(frozen=True)
class WorkflowEditContext:
    """Input bundle for the copilot edit stage.

    Built by the ``copilot-edit`` route from the user's natural-language
    ``instruction``, the current ``manifest`` (the open canvas), and a
    ``grounding`` bundle listing registry artifacts the editor may reference
    (tools / skills / eval datasets) — Lakeflow's "knows your data" applied to
    "knows your artifacts", so the model proposes real, *resolvable* refs
    rather than inventing them.
    """

    instruction: str
    manifest: dict[str, Any]
    grounding: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowGenerationContext:
    """Input bundle for the plan-to-build generation stage.

    Built by the ``plan-build`` route from the user's plain-language ``goal``
    (what the workflow should *do*), the current ``manifest`` (the open canvas —
    used as the diff base and for its identity fields ``workflow_id`` /
    ``schema_version`` / ``name``, *not* preserved node-for-node), and the same
    ``grounding`` bundle the copilot uses so generated nodes reference real,
    resolvable registry refs. Unlike :class:`WorkflowEditContext`, the model is
    asked to *author the graph toward the goal* rather than apply a minimal edit.
    """

    goal: str
    manifest: dict[str, Any]
    grounding: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class TriageContext:
    """Input bundle for the triage classifier.

    Built by :func:`caliber.orchestrator.triage.run_triage` from the source
    verification item. ``agent_has_skills`` tells the classifier whether the
    agent references any skills (so it can prefer ``artifact_type="skill"`` for
    tool-use failures); the deterministic skill *resolution* still happens in
    triage so the model never invents a skill name.
    """

    agent_id: str
    item_id: str
    category: str
    severity: str
    free_text: str
    agent_has_skills: bool = False


@dataclass(frozen=True)
class EvidenceContext:
    """Input bundle for the diagnosis stage.

    Built by :func:`caliber.orchestrator.diagnosis.run_diagnosis` from the
    verification item plus the evidence summary recorded by the evidence
    stage. Passing a typed dataclass (rather than an open dict) makes the
    contract between stages and providers explicit and refactorable.
    """

    agent_id: str
    item_id: str
    category: str
    severity: str
    free_text: str
    trace_id: str | None = None
    session_id: str | None = None
    evidence_summary: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateContext:
    """Input bundle for the candidate-generation stage.

    Built by :func:`caliber.orchestrator.candidate.run_candidate`. The
    ``optimizer_type`` field tells the production provider which agent to
    invoke (MetaPrompt for now; GEPA, TextGrad, etc. as those land).
    ``current_artifact_content`` is the current value of the artifact the
    job targets — typically the prompt aliased ``@prod``. May be ``None``
    on cold-start deployments.

    ``review_notes`` carries reviewer guidance from a prior approval that
    request-changes resolved with — only populated on retry passes. When
    set, the production provider weaves it into the prompt so the next
    candidate addresses the reviewer's concerns.

    Skill-specific fields (populated only when ``artifact_type == "skill"``):

    * ``skill_name`` — the kebab-case skill name, used to identify the
      skill in the database and in promotion.
    * ``skill_metadata`` — the skill's ``skill_metadata`` JSON bag (author,
      documentation URL, etc.).
    * ``allowed_tools`` — the skill's tool restrictions (e.g.
      ``"Bash(python:*) WebFetch"``). ``None`` = unrestricted.
    * ``depends_on`` — names of skills this one composes with.
    * ``affected_agent_ids`` — agents that reference this skill, for context
      in multi-agent eval.
    """

    agent_id: str
    job_id: str
    artifact_type: str
    optimizer_type: str
    diagnosis: Diagnosis
    current_artifact_content: str | None = None
    review_notes: str | None = None

    # Skill-specific fields — None for prompt jobs.
    skill_name: str | None = None
    skill_metadata: dict[str, object] | None = None
    allowed_tools: str | None = None
    depends_on: list[str] | None = None
    affected_agent_ids: list[str] | None = None

    # GEPA-specific fields — populated when optimizer_type == "GEPA".
    pareto_dims: list[str] | None = None
    population_size: int | None = None
    generations: int | None = None

    # DSPy-specific fields — populated when ``optimizer_type`` starts with
    # ``"DSPy"`` (e.g. ``"DSPyBootstrapFewShot"``). ``trainset`` is the list of
    # ``{"input": ..., "expected": ...}`` rows the optimizer bootstraps few-shot
    # demonstrations from (loaded from the agent's eval dataset by
    # ``run_candidate``). The demo-count knobs default to ``None`` so the
    # provider falls back to its config-level defaults; an agent's
    # ``optimizer_config`` may override them per-agent.
    trainset: list[dict[str, Any]] | None = None
    dspy_max_bootstrapped_demos: int | None = None
    dspy_max_labeled_demos: int | None = None
    # MIPROv2 budget preset ("light"/"medium"/"heavy"); None → provider default.
    dspy_mipro_auto: str | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Cost / latency telemetry returned alongside structured outputs.

    Stored on ``caliber_refinement_jobs.total_tokens`` and ``cost_usd`` so
    the cost dashboards have real numbers to
    report. Providers that can't report all fields populate what they have.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# The Protocol itself
# ---------------------------------------------------------------------------


class LLMProviderError(Exception):
    """Raised when an LLM provider call fails after retries.

    Stages catch this and mark their job ``failed`` with the message; the
    worker writes the audit row.
    """


class LLMProvider(Protocol):
    """One method per LLM-driven orchestrator stage.

    Implementations are responsible for prompt construction, retry, and
    error normalization (always raise :class:`LLMProviderError` on failure,
    never the raw provider-specific exception type — that's what keeps the
    rest of CALIBER decoupled from the SDK).
    """

    def classify_triage(self, context: TriageContext) -> tuple[TriageClassification, LLMUsage]:
        """Classify a verification item into a failure cluster + artifact type.

        Runs first in the pipeline. The triage stage uses the result to set the
        job's preliminary ``artifact_type`` (and, for skill/tool-use failures,
        to drive the deterministic skill resolution). Implementations reason
        over the item's category + free-text; the test fake returns a
        deterministic classification.

        Returns
        -------
        tuple
            ``(TriageClassification, LLMUsage)``.

        Raises
        ------
        LLMProviderError
            On any LLM-side failure. The triage stage catches this and falls
            back to its deterministic heuristic rather than failing the job.
        """
        ...

    def diagnose(self, evidence: EvidenceContext) -> tuple[Diagnosis, LLMUsage]:
        """Produce a structured diagnosis from collected evidence.

        Returns
        -------
        tuple
            ``(Diagnosis, LLMUsage)`` — the structured root-cause payload
            and the telemetry the orchestrator should record.

        Raises
        ------
        LLMProviderError
            On any LLM-side failure (auth, rate limit, malformed output,
            timeout). The orchestrator marks the job failed; partial state
            is not committed.
        """
        ...

    def generate_candidate(self, context: CandidateContext) -> tuple[PromptCandidate, LLMUsage]:
        """Generate a candidate fix for the diagnosed issue.

        Production implementations dispatch on ``context.optimizer_type``
        to pick the right strategy (MetaPrompt rewriter, GEPA evolver,
        DSPyBootstrapFewShot demo-selector, etc.). The test fake ignores the
        ``optimizer_type`` choice of engine and returns a canned candidate
        (still reflecting the optimizer name so wiring is assertable).

        Returns
        -------
        tuple
            ``(PromptCandidate, LLMUsage)`` — the new artifact content + telemetry.

        Raises
        ------
        LLMProviderError
            On any LLM-side failure. Same contract as :meth:`diagnose`.
        """
        ...

    def propose_workflow_edit(self, context: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        """Propose a natural-language edit to a workflow manifest (modify-in-place).

        Returns the *full* edited manifest (not a patch), grounded in
        ``context.grounding`` so referenced artifacts resolve. The route
        validates the result compiles and diffs it against the base for an
        accept/reject UI; nothing is persisted by the provider.

        Returns
        -------
        tuple
            ``(WorkflowEdit, LLMUsage)`` — the proposed manifest + telemetry.

        Raises
        ------
        LLMProviderError
            On any LLM-side failure. Same contract as :meth:`diagnose`.
        """
        ...

    def generate_workflow_from_goal(
        self, context: WorkflowGenerationContext
    ) -> tuple[WorkflowEdit, LLMUsage]:
        """Author a workflow manifest from a plain-language goal (plan-to-build).

        The blank-slate sibling of :meth:`propose_workflow_edit`: instead of a
        minimal edit it generates the *full* graph toward ``context.goal``,
        preserving only the base manifest's identity (``workflow_id`` /
        ``schema_version``). Returns the same ``(WorkflowEdit, LLMUsage)`` shape
        so the route can diff + validate it through the copilot path. Raises
        :class:`LLMProviderError` on any LLM-side failure.
        """
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_provider(config: CaliberConfig) -> LLMProvider:
    """Instantiate the configured LLM provider.

    ``CaliberConfig.llm_provider`` selects the base implementation:

    * ``"fake"`` — :class:`FakeLLMProvider`, used by tests and by default
      so that ``mlflow server --app-name caliber`` boots without an API key.
    * ``"openai"`` — :class:`OpenAIAgentsLLMProvider`, requires the
      ``caliber[llm]`` install extra and an API key in the env var
      named by ``config.llm_api_key_env``.

    When ``config.llm_circuit_breaker_enabled`` is true, the result is
    wrapped with :class:`CircuitBreakerLLMProvider` so a misbehaving
    provider trips the breaker and the worker re-queues jobs without
    consuming retry budget.
    """
    base = _build_base_provider(config)
    # Lazy import: ``circuit_breaker`` imports from this module, so a
    # top-level import would be circular.
    from caliber.llm.circuit_breaker import maybe_wrap  # noqa: PLC0415

    return maybe_wrap(
        base,
        enabled=config.llm_circuit_breaker_enabled,
        failure_threshold=config.llm_circuit_failure_threshold,
        window_seconds=config.llm_circuit_window_seconds,
        open_duration_seconds=config.llm_circuit_open_duration_seconds,
    )


def _build_base_provider(config: CaliberConfig) -> LLMProvider:
    """Construct the unwrapped provider matching ``config.llm_provider``.

    Split from :func:`build_provider` so the breaker wrap-or-skip
    decision lives in one place and tests can target the raw provider
    by calling this helper directly.
    """
    provider = config.llm_provider.lower()

    if provider == "fake":
        # Lazy import deliberate: ``caliber.llm.fake`` imports from this
        # module, so a top-level import would be circular.
        from caliber.llm.fake import FakeLLMProvider  # noqa: PLC0415

        return FakeLLMProvider()

    if provider == "openai":
        # Lazy import deliberate: ``caliber.llm.openai_agents`` imports from
        # this module (circular), and its lazy import of ``agents`` would also
        # blow up without the ``[llm]`` extra installed.
        from caliber.llm.openai_agents import OpenAIAgentsLLMProvider  # noqa: PLC0415
        from caliber.secrets import resolve_secret  # noqa: PLC0415

        api_key = resolve_secret(config.llm_api_key_env)
        if not api_key:
            raise LLMProviderError(
                f"llm_provider='openai' but secret source {config.llm_api_key_env!r} "
                "did not resolve to a value"
            )
        return OpenAIAgentsLLMProvider(
            api_key=api_key,
            diagnosis_model=config.llm_diagnosis_model,
            gepa_reflection_model=config.gepa_reflection_model,
            gepa_max_metric_calls=config.gepa_max_metric_calls,
            dspy_max_bootstrapped_demos=config.dspy_max_bootstrapped_demos,
            dspy_max_labeled_demos=config.dspy_max_labeled_demos,
            dspy_mipro_auto=config.dspy_mipro_auto,
            allow_flagged_dspy_optimizers=config.allow_flagged_dspy_optimizers,
        )

    raise LLMProviderError(f"unknown llm_provider {config.llm_provider!r}")
