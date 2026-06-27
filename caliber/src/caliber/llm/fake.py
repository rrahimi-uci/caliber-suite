"""Deterministic LLM provider for tests and demos.

Every method returns a canned, structurally-valid response and records the
inputs it was called with so tests can assert on prompt construction. The
default response shape is the simplest thing that lets each orchestrator
stage satisfy its state-machine contract.

To exercise specific code paths (e.g. low-confidence diagnosis triggering
GEPA later, or a candidate generator that fails), tests can override the
canned response or inject a side-effecting callable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    EvidenceContext,
    LLMUsage,
    PromptCandidate,
    TriageClassification,
    TriageContext,
    WorkflowEdit,
    WorkflowEditContext,
    WorkflowGenerationContext,
)


@dataclass
class FakeLLMProvider:
    """Deterministic stand-in for tests.

    Attributes
    ----------
    diagnose_response:
        Optional override of the canned :class:`Diagnosis` returned for
        every ``diagnose()`` call.
    diagnose_usage:
        Optional override of the :class:`LLMUsage` returned for every
        ``diagnose()`` call.
    diagnose_callable:
        Optional callable. Same shape as ``diagnose()``; takes precedence
        over ``diagnose_response`` / ``diagnose_usage`` when set.
    candidate_response / candidate_usage / candidate_callable:
        Same three-knob pattern for ``generate_candidate()``.
    diagnose_calls / candidate_calls:
        Read-only lists of every context the corresponding method was
        called with — for asserting prompt-construction inputs.
    """

    diagnose_response: Diagnosis | None = None
    diagnose_usage: LLMUsage | None = None
    diagnose_callable: Callable[[EvidenceContext], tuple[Diagnosis, LLMUsage]] | None = None

    candidate_response: PromptCandidate | None = None
    candidate_usage: LLMUsage | None = None
    candidate_callable: Callable[[CandidateContext], tuple[PromptCandidate, LLMUsage]] | None = None

    edit_response: WorkflowEdit | None = None
    edit_usage: LLMUsage | None = None
    edit_callable: Callable[[WorkflowEditContext], tuple[WorkflowEdit, LLMUsage]] | None = None

    gen_response: WorkflowEdit | None = None
    gen_usage: LLMUsage | None = None
    gen_callable: (
        Callable[[WorkflowGenerationContext], tuple[WorkflowEdit, LLMUsage]] | None
    ) = None

    triage_response: TriageClassification | None = None
    triage_usage: LLMUsage | None = None
    triage_callable: Callable[[TriageContext], tuple[TriageClassification, LLMUsage]] | None = None

    diagnose_calls: list[EvidenceContext] = field(default_factory=list)
    candidate_calls: list[CandidateContext] = field(default_factory=list)
    edit_calls: list[WorkflowEditContext] = field(default_factory=list)
    gen_calls: list[WorkflowGenerationContext] = field(default_factory=list)
    triage_calls: list[TriageContext] = field(default_factory=list)

    # Backwards-compatible alias so existing tests using ``provider.calls``
    # (recorded diagnose calls) still work.
    @property
    def calls(self) -> list[EvidenceContext]:
        return self.diagnose_calls

    def classify_triage(self, context: TriageContext) -> tuple[TriageClassification, LLMUsage]:
        self.triage_calls.append(context)
        if self.triage_callable is not None:
            return self.triage_callable(context)
        classification = self.triage_response or _default_triage(context)
        usage = self.triage_usage or _default_triage_usage()
        return classification, usage

    def diagnose(self, evidence: EvidenceContext) -> tuple[Diagnosis, LLMUsage]:
        self.diagnose_calls.append(evidence)
        if self.diagnose_callable is not None:
            return self.diagnose_callable(evidence)
        diagnosis = self.diagnose_response or _default_diagnosis(evidence)
        usage = self.diagnose_usage or _default_diagnose_usage()
        return diagnosis, usage

    def generate_candidate(self, context: CandidateContext) -> tuple[PromptCandidate, LLMUsage]:
        self.candidate_calls.append(context)
        if self.candidate_callable is not None:
            return self.candidate_callable(context)
        candidate = self.candidate_response or _default_candidate(context)
        usage = self.candidate_usage or _default_candidate_usage()
        return candidate, usage

    def propose_workflow_edit(self, context: WorkflowEditContext) -> tuple[WorkflowEdit, LLMUsage]:
        self.edit_calls.append(context)
        if self.edit_callable is not None:
            return self.edit_callable(context)
        edit = self.edit_response or _default_workflow_edit(context)
        usage = self.edit_usage or _default_edit_usage()
        return edit, usage

    def generate_workflow_from_goal(
        self, context: WorkflowGenerationContext
    ) -> tuple[WorkflowEdit, LLMUsage]:
        self.gen_calls.append(context)
        if self.gen_callable is not None:
            return self.gen_callable(context)
        edit = self.gen_response or _default_workflow_generation(context)
        usage = self.gen_usage or _default_edit_usage()
        return edit, usage


def _default_triage(context: TriageContext) -> TriageClassification:
    """Deterministic triage mirroring the heuristic clusters.

    Tool-use failures route to ``skill`` when the agent references skills, else
    ``prompt``; everything else (hallucination, context drift, …) to ``prompt``.
    """
    category = (context.category or "").lower()
    if category in {"tool_use", "tool_calling"}:
        artifact_type = "skill" if context.agent_has_skills else "prompt"
        return TriageClassification(
            cluster="tool_use",
            artifact_type=artifact_type,
            confidence=0.6,
            rationale="[fake] tool-use failure",
        )
    if category in {"hallucination", "factual"}:
        return TriageClassification(
            cluster="hallucination",
            artifact_type="prompt",
            confidence=0.6,
            rationale="[fake] hallucination",
        )
    if category in {"context_drift", "memory"}:
        return TriageClassification(
            cluster="context_drift",
            artifact_type="prompt",
            confidence=0.6,
            rationale="[fake] context drift",
        )
    return TriageClassification(
        cluster="other", artifact_type="prompt", confidence=0.4, rationale="[fake] uncategorized"
    )


def _default_triage_usage() -> LLMUsage:
    return LLMUsage(input_tokens=300, output_tokens=40, cost_usd=0.0008)


def _default_diagnosis(evidence: EvidenceContext) -> Diagnosis:
    return Diagnosis(
        root_cause=f"[fake] inferred root cause for category={evidence.category!r}",
        affected_components=["prompt"],
        confidence=0.75,
        alternatives=[],
    )


def _default_diagnose_usage() -> LLMUsage:
    return LLMUsage(input_tokens=500, output_tokens=120, cost_usd=0.0015)


def _default_candidate(context: CandidateContext) -> PromptCandidate:
    """Build a structurally-valid candidate from the context.

    The fake's body deliberately references the upstream diagnosis (and
    reviewer notes, on retry) so tests can verify the wiring
    (context → provider → candidate) without having to mock prompt
    construction.
    """
    artifact_label = context.artifact_type
    if context.skill_name:
        artifact_label = f"skill:{context.skill_name}"

    # Optimizer-specific candidates carry extra metadata so tests can assert
    # the optimizer choice (and its inputs) flowed through to the provider.
    optimizer_suffix = ""
    if context.optimizer_type == "GEPA":
        optimizer_suffix = (
            "# GEPA: genetic-Pareto evolution applied\n"
            f"# Confidence: {context.diagnosis.confidence}\n"
            f"# Alternatives considered: {len(context.diagnosis.alternatives)}\n"
        )
    elif context.optimizer_type == "DSPyBootstrapFewShot":
        optimizer_suffix = (
            "# DSPyBootstrapFewShot: few-shot demonstrations selected\n"
            f"# Trainset examples available: {len(context.trainset or [])}\n"
        )
    elif context.optimizer_type == "DSPyMIPRO":
        optimizer_suffix = (
            "# DSPyMIPRO: instruction + few-shot demonstrations optimized\n"
            f"# Trainset examples available: {len(context.trainset or [])}\n"
            f"# MIPRO budget preset: {context.dspy_mipro_auto or 'light'}\n"
        )

    if context.current_artifact_content:
        new_content = (
            f"{context.current_artifact_content}\n\n"
            f"# Refinement added by {context.optimizer_type}\n"
            f"# Diagnosis: {context.diagnosis.root_cause}\n"
        )
        if context.skill_name:
            new_content += f"# Skill: {context.skill_name}\n"
            if context.allowed_tools:
                new_content += f"# Allowed tools: {context.allowed_tools}\n"
        new_content += optimizer_suffix
    else:
        # Cold-start path — no existing artifact.
        new_content = (
            f"# Bootstrapped by {context.optimizer_type} for {artifact_label}.\n"
            f"# Initial directive derived from diagnosis: "
            f"{context.diagnosis.root_cause}\n" + optimizer_suffix
        )
    if context.review_notes:
        # Retry pass — surface reviewer guidance so tests can assert it
        # flowed through.
        new_content = f"{new_content}# Reviewer notes: {context.review_notes}\n"
    return PromptCandidate(
        artifact_type=context.artifact_type,
        content=new_content,
        rationale=f"[fake] applying {context.optimizer_type} pattern",
        diff_summary="+3 / -0 lines",
    )


def _default_candidate_usage() -> LLMUsage:
    return LLMUsage(input_tokens=900, output_tokens=300, cost_usd=0.004)


def _default_workflow_edit(context: WorkflowEditContext) -> WorkflowEdit:
    """Return the manifest **unchanged** — the no-footgun dev/test default.

    The fake provider cannot synthesize a real edit, so it echoes the base
    manifest back. Accepting it in the Studio is therefore a safe no-op (an
    empty graph diff) rather than replacing the user's workflow with a canned
    stub. Real natural-language editing requires ``llm_provider='openai'``
    (optionally fronted by the MLflow AI Gateway).
    """
    return WorkflowEdit(
        manifest=dict(context.manifest),
        summary="No LLM configured — manifest returned unchanged.",
        rationale=(
            "The fake LLM provider does not synthesize workflow edits. Set "
            "llm_provider='openai' (optionally via the MLflow AI Gateway) to "
            "enable natural-language workflow authoring."
        ),
    )


def _default_edit_usage() -> LLMUsage:
    return LLMUsage(input_tokens=0, output_tokens=0, cost_usd=0.0)


def _default_workflow_generation(context: WorkflowGenerationContext) -> WorkflowEdit:
    """Return the base manifest **unchanged** — the no-footgun dev/test default.

    The fake provider can't author a workflow from a goal, so it echoes the
    base back (an empty graph diff → Accept is a safe no-op) rather than
    scaffolding a canned graph onto the canvas. Real plan-to-build requires
    ``llm_provider='openai'`` (optionally fronted by the MLflow AI Gateway).
    """
    return WorkflowEdit(
        manifest=dict(context.manifest),
        summary="No LLM configured — manifest returned unchanged.",
        rationale=(
            "The fake LLM provider does not author workflows from a goal. Set "
            "llm_provider='openai' (optionally via the MLflow AI Gateway) to "
            "enable plan-to-build workflow authoring."
        ),
    )
