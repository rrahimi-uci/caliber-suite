"""Workflow-aware CALIBER refinement primitives (plan §12, §17).

This is the engine behind "CALIBER refines the *workflow* that produced the
failure," not just a prompt. It provides three pure, deterministic, testable
steps that the orchestrator's diagnosis / candidate / eval stages call when a
verification item carries workflow/node metadata:

1. :func:`localize_failure` — map collected evidence to affected components and
   the stable ids they live at (node/edge/tool). Mirrors the diagnosis output
   shape in §17.2.
2. :func:`generate_workflow_patch` — turn a diagnosis into *semantic* patch ops
   (id-keyed, never positional — §17.3) plus the materialized candidate
   manifest and a human-readable graph diff.
3. :func:`evaluate_candidate` — compile baseline + candidate, replay eval
   examples through both via the runtime, score per dimension, compute deltas,
   and apply the deploy-gate thresholds (§17.4).

These are deliberately provider-free so they're deterministic in tests. A
production LLM provider can enrich :func:`localize_failure`/
:func:`generate_workflow_patch` (e.g. richer prompt rewrites), but the gating
contract stays the same.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from caliber.workflows.compiler import CompileError, compile_workflow
from caliber.workflows.diff import compute_graph_diff
from caliber.workflows.manifest import WorkflowManifest, parse_manifest
from caliber.workflows.patch import PatchError, apply_patch
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    WorkflowExecutor,
    WorkflowRunResult,
    execute,
)
from caliber.workflows.tools import ToolResolver

# Vocabulary of affected components a workflow diagnosis can name (plan §11.3).
COMPONENT_PROMPT = "prompt"
COMPONENT_TOOL_CONTRACT = "tool_contract"
COMPONENT_WORKFLOW_EDGE = "workflow_edge"
COMPONENT_GUARDRAIL = "guardrail"
ManifestExecutorFactory = Callable[[dict[str, Any]], WorkflowExecutor]


# ---------------------------------------------------------------------------
# 1. Diagnosis localization
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDiagnosis:
    root_cause: str
    affected_components: list[str]
    localized_to: dict[str, Any]
    recommended_patch_type: str  # workflow_manifest | prompt
    confidence: float = 0.7
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "affected_components": self.affected_components,
            "localized_to": self.localized_to,
            "recommended_patch_type": self.recommended_patch_type,
            "confidence": self.confidence,
            "alternatives": self.alternatives,
        }


def localize_failure(evidence: dict[str, Any]) -> WorkflowDiagnosis:
    """Map evidence to affected components + stable ids (deterministic).

    ``evidence`` keys (all optional):

    * ``category`` — feedback category (``tool_use`` / ``handoff`` / ``guardrail``
      / ``hallucination`` / ...).
    * ``node_id`` / ``edge_id`` — where the failure was observed.
    * ``required_tools`` / ``observed_tool_calls`` — to detect a missing
      grounding tool call.
    * ``wrong_handoff`` / ``guardrail_false_positive`` / ``hallucination`` —
      explicit signals.
    * ``workflow_id`` / ``workflow_version_id`` — localization scope.
    """
    category = str(evidence.get("category", "")).lower()
    node_id = evidence.get("node_id")
    edge_id = evidence.get("edge_id")
    required = set(evidence.get("required_tools", []) or [])
    observed = set(evidence.get("observed_tool_calls", []) or [])

    components: set[str] = set()
    tool_refs: list[str] = []

    missing_tools = sorted(required - observed)
    if missing_tools or (category == "tool_use" and evidence.get("tool_called") is False):
        components.add(COMPONENT_TOOL_CONTRACT)
        tool_refs = missing_tools

    if bool(evidence.get("wrong_handoff")) or category == "handoff":
        components.add(COMPONENT_WORKFLOW_EDGE)

    if bool(evidence.get("guardrail_false_positive")) or category == "guardrail":
        components.add(COMPONENT_GUARDRAIL)

    if bool(evidence.get("hallucination")) or category in ("hallucination", "prompt"):
        components.add(COMPONENT_PROMPT)

    if not components:
        components.add(COMPONENT_PROMPT)

    manifest_components = {COMPONENT_TOOL_CONTRACT, COMPONENT_WORKFLOW_EDGE, COMPONENT_GUARDRAIL}
    recommended = "workflow_manifest" if components & manifest_components else "prompt"

    localized_to: dict[str, Any] = {
        "workflow_id": evidence.get("workflow_id"),
        "workflow_version_id": evidence.get("workflow_version_id"),
        "node_ids": [node_id] if node_id else [],
        "edge_ids": [edge_id] if edge_id else [],
        "tool_refs": tool_refs,
    }
    return WorkflowDiagnosis(
        root_cause=str(evidence.get("free_text") or evidence.get("root_cause") or "")
        or "workflow-level failure",
        affected_components=sorted(components),
        localized_to=localized_to,
        recommended_patch_type=recommended,
        confidence=float(evidence.get("confidence", 0.8)),
    )


# ---------------------------------------------------------------------------
# 2. Candidate patch generation
# ---------------------------------------------------------------------------


@dataclass
class WorkflowPatchCandidate:
    patch_kind: str  # workflow_manifest | prompt
    semantic_ops: list[dict[str, Any]]
    candidate_manifest: dict[str, Any]
    summary: str
    graph_diff: dict[str, Any]
    prompt_suggestion: str | None = None


def generate_workflow_patch(
    manifest: WorkflowManifest,
    diagnosis: WorkflowDiagnosis,
    *,
    resolver: ToolResolver | None = None,
) -> WorkflowPatchCandidate:
    """Produce semantic patch ops + materialized candidate from a diagnosis.

    Deterministic, rule-based generation for the MVP failure classes:

    * ``tool_contract`` (a grounding tool wasn't called) → insert a
      ``tool_required_before_claim`` guardrail after the offending agent and
      record the tool constraint.
    * ``workflow_edge`` (wrong handoff/route) → add a handoff to the suggested
      target.
    * ``guardrail`` (false positive) → relax the guardrail's failure behavior.
    * ``prompt`` only → no manifest change; a prompt rewrite suggestion.
    """
    base = manifest.to_dict()
    components = set(diagnosis.affected_components)
    node_ids = diagnosis.localized_to.get("node_ids") or []
    target_node = node_ids[0] if node_ids else _first_agent_id(manifest)
    ops: list[dict[str, Any]] = []
    summary_parts: list[str] = []
    prompt_suggestion: str | None = None

    if COMPONENT_TOOL_CONTRACT in components and target_node:
        tool = (diagnosis.localized_to.get("tool_refs") or ["lookup_policy"])[0]
        guard_id = f"{target_node}_grounding_guard"
        ops.append(
            {
                "op": "add_node_after",
                "target_node_id": target_node,
                "edge_id": f"e_{target_node}_{guard_id}",
                "node": {
                    "id": guard_id,
                    "type": "guardrail",
                    "mode": "post_agent",
                    "inputs": {"response": {"type": "string"}},
                    "outputs": {"passthrough": {"type": "string"}},
                    "on_failure": "block",
                    "checks": [
                        {
                            "tool_required_before_claim": {
                                "tool": tool,
                                "categories": ["refund_policy", "warranty_policy"],
                            }
                        }
                    ],
                },
            }
        )
        ops.append(
            {
                "op": "update_tool_constraint",
                "target_node_id": target_node,
                "tool_ref": tool,
                "constraint": "required_before_claim",
            }
        )
        summary_parts.append(
            f"Insert a tool-grounding guardrail after {target_node!r} requiring {tool!r}."
        )

    if COMPONENT_WORKFLOW_EDGE in components and target_node:
        suggested = diagnosis.localized_to.get("suggested_handoff")
        if suggested:
            ops.append(
                {
                    "op": "update_node_field",
                    "target_node_id": target_node,
                    "field_path": "handoffs",
                    "value": [{"target": suggested, "description": "route per diagnosis"}],
                }
            )
            summary_parts.append(f"Route {target_node!r} to hand off to {suggested!r}.")

    if COMPONENT_GUARDRAIL in components and target_node:
        ops.append(
            {
                "op": "update_node_field",
                "target_node_id": target_node,
                "field_path": "on_failure",
                "value": "redact",
            }
        )
        summary_parts.append(f"Relax guardrail {target_node!r} failure behavior to redact.")

    if not ops:
        prompt_suggestion = (
            "Tighten the agent instructions: require tool-grounded answers and "
            "forbid unsupported policy claims."
        )
        return WorkflowPatchCandidate(
            patch_kind="prompt",
            semantic_ops=[],
            candidate_manifest=base,
            summary=prompt_suggestion,
            graph_diff=compute_graph_diff(manifest, manifest),
            prompt_suggestion=prompt_suggestion,
        )

    candidate = apply_patch(base, ops, resolver=resolver)
    return WorkflowPatchCandidate(
        patch_kind="workflow_manifest",
        semantic_ops=ops,
        candidate_manifest=candidate.to_dict(),
        summary=" ".join(summary_parts),
        graph_diff=compute_graph_diff(manifest, candidate),
    )


def _first_agent_id(manifest: WorkflowManifest) -> str | None:
    from caliber.workflows.manifest import AgentNode  # noqa: PLC0415

    for nid, node in manifest.nodes.items():
        if isinstance(node, AgentNode):
            return nid
    return None


# ---------------------------------------------------------------------------
# 3. Candidate evaluation (baseline vs candidate)
# ---------------------------------------------------------------------------

# A scorer maps a single run result to per-dimension scores.
RunScorer = Callable[[WorkflowRunResult], dict[str, float]]


def default_run_scorer(result: WorkflowRunResult) -> dict[str, float]:
    """Quality / tool-adherence / completion proxies from a single run."""
    completed = 1.0 if result.status == "completed" else 0.0
    guardrails_passed = 1.0 if all(g.get("passed", True) for g in result.guardrail_results) else 0.0
    quality = completed * (0.5 + 0.5 * guardrails_passed)
    return {
        "completion_rate": completed,
        "tool_adherence": guardrails_passed if completed else 0.0,
        "quality": quality,
    }


@dataclass
class WorkflowEvalResult:
    passed: bool
    baseline_scores: dict[str, float]
    candidate_scores: dict[str, float]
    deltas: dict[str, float]
    n_examples: int
    gate: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "baseline_scores": self.baseline_scores,
            "candidate_scores": self.candidate_scores,
            "deltas": self.deltas,
            "n_examples": self.n_examples,
            "gate": self.gate,
            "error": self.error,
        }


def _mean_scores(per_run: list[dict[str, float]]) -> dict[str, float]:
    if not per_run:
        return {}
    keys = per_run[0].keys()
    return {k: sum(r[k] for r in per_run) / len(per_run) for k in keys}


def _replay(
    manifest_dict: dict[str, Any],
    inputs: Sequence[str],
    *,
    resolver: ToolResolver,
    executor: WorkflowExecutor,
    scorer: RunScorer,
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, float]:
    result = compile_workflow(parse_manifest(manifest_dict), resolver=resolver, version="eval")
    plan = RuntimePlan(
        ir=result.ir,
        resolver=resolver,
        knowledge_query_runner=knowledge_query_runner,
        knowledge_build_runner=knowledge_build_runner,
    )
    per_run = [
        _invoke_scorer(scorer, execute(plan, text, executor=executor), text) for text in inputs
    ]
    return _mean_scores(per_run)


def _invoke_scorer(
    scorer: RunScorer, result: WorkflowRunResult, input_text: str
) -> dict[str, float]:
    """Call ``scorer`` with the run input when it accepts an ``input_text`` kwarg.

    Lets an input-aware scorer (the LLM judge) assess on-topic correctness while
    plain scorers like ``default_run_scorer`` keep their ``(result)`` signature.
    """
    try:
        params = inspect.signature(scorer).parameters
    except (TypeError, ValueError):
        return scorer(result)
    if "input_text" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return scorer(result, input_text=input_text)  # type: ignore[call-arg]
    return scorer(result)


def _resolve_manifest_executor(
    manifest_dict: dict[str, Any],
    *,
    executor: WorkflowExecutor | None,
    executor_factory: ManifestExecutorFactory | None = None,
) -> WorkflowExecutor:
    """Resolve the executor used to replay one workflow manifest."""

    if executor_factory is not None:
        return executor_factory(manifest_dict)
    return executor or FakeWorkflowExecutor()


def evaluate_candidate(
    baseline_manifest: dict[str, Any],
    candidate_manifest: dict[str, Any],
    inputs: Sequence[str],
    *,
    resolver: ToolResolver,
    thresholds: dict[str, float] | None = None,
    baseline_executor: WorkflowExecutor | None = None,
    candidate_executor: WorkflowExecutor | None = None,
    baseline_executor_factory: ManifestExecutorFactory | None = None,
    candidate_executor_factory: ManifestExecutorFactory | None = None,
    scorer: RunScorer | None = None,
    baseline_scores: dict[str, float] | None = None,
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> WorkflowEvalResult:
    """Compile + replay baseline and candidate; gate on score deltas (plan §17.4).

    ``thresholds`` keys: ``min_overall_delta`` (default 0.0) on the ``quality``
    dimension, and ``max_tone_regression`` (default ∞) limiting how far the
    ``tone`` dimension may drop. Pass ``baseline_scores`` to reuse a cached
    baseline run (plan §26.4 "cached baselines").
    """
    thresholds = thresholds or {}
    scorer = scorer or default_run_scorer

    try:
        resolved_candidate_executor = _resolve_manifest_executor(
            candidate_manifest,
            executor=candidate_executor,
            executor_factory=candidate_executor_factory,
        )
        cand_scores = _replay(
            candidate_manifest,
            inputs,
            resolver=resolver,
            executor=resolved_candidate_executor,
            scorer=scorer,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
        )
    except (CompileError, PatchError, ValueError) as exc:
        return WorkflowEvalResult(
            passed=False,
            baseline_scores=baseline_scores or {},
            candidate_scores={},
            deltas={},
            n_examples=len(inputs),
            gate={"passed": False, "reason": f"candidate compile/replay failed: {exc}"},
            error=str(exc),
        )

    if baseline_scores is None:
        try:
            resolved_baseline_executor = _resolve_manifest_executor(
                baseline_manifest,
                executor=baseline_executor,
                executor_factory=baseline_executor_factory,
            )
            base_scores = _replay(
                baseline_manifest,
                inputs,
                resolver=resolver,
                executor=resolved_baseline_executor,
                scorer=scorer,
                knowledge_query_runner=knowledge_query_runner,
                knowledge_build_runner=knowledge_build_runner,
            )
        except (CompileError, PatchError, ValueError) as exc:
            return WorkflowEvalResult(
                passed=False,
                baseline_scores={},
                candidate_scores=cand_scores,
                deltas={},
                n_examples=len(inputs),
                gate={"passed": False, "reason": f"baseline compile/replay failed: {exc}"},
                error=str(exc),
            )
    else:
        base_scores = baseline_scores

    dims = set(base_scores) | set(cand_scores)
    deltas = {d: cand_scores.get(d, 0.0) - base_scores.get(d, 0.0) for d in dims}

    min_overall_delta = float(thresholds.get("min_overall_delta", 0.0))
    max_tone_regression = float(thresholds.get("max_tone_regression", float("inf")))

    overall_delta = deltas.get("quality", 0.0)
    tone_delta = deltas.get("tone", 0.0)
    # Tolerate float noise so an exact threshold (e.g. tone -0.01 vs limit 0.01)
    # counts as within tolerance, matching the §16.7.8 example.
    eps = 1e-9
    reasons: list[str] = []
    passed = True
    if overall_delta < min_overall_delta - eps:
        passed = False
        reasons.append(
            f"quality delta {overall_delta:+.3f} < min_overall_delta {min_overall_delta}"
        )
    if tone_delta < -max_tone_regression - eps:
        passed = False
        reasons.append(
            f"tone regression {tone_delta:+.3f} exceeds max_tone_regression {max_tone_regression}"
        )

    return WorkflowEvalResult(
        passed=passed,
        baseline_scores=base_scores,
        candidate_scores=cand_scores,
        deltas=deltas,
        n_examples=len(inputs),
        gate={"passed": passed, "reasons": reasons, "thresholds": thresholds},
    )


__all__ = [
    "COMPONENT_GUARDRAIL",
    "COMPONENT_PROMPT",
    "COMPONENT_TOOL_CONTRACT",
    "COMPONENT_WORKFLOW_EDGE",
    "WorkflowDiagnosis",
    "WorkflowEvalResult",
    "WorkflowPatchCandidate",
    "default_run_scorer",
    "evaluate_candidate",
    "generate_workflow_patch",
    "localize_failure",
]
