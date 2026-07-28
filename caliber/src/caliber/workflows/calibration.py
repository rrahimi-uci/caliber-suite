"""Deterministic workflow calibration helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberVerificationItem,
)
from caliber.workflows.compiler import CompileError, compile_workflow
from caliber.workflows.diff import compute_graph_diff
from caliber.workflows.manifest import (
    AgentNode,
    GuardrailNode,
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.patch import PatchError, apply_patch
from caliber.workflows.refinement import (
    COMPONENT_TOOL_CONTRACT,
    COMPONENT_WORKFLOW_EDGE,
    RunScorer,
    WorkflowDiagnosis,
    _invoke_scorer,
    generate_workflow_patch,
)
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    WorkflowExecutor,
    WorkflowRunResult,
    execute,
)
from caliber.workflows.tools import ToolResolver
from caliber.workflows.validation import validate_manifest

# A calibration candidate may bundle at most this many semantic ops; larger
# diffs are skipped to keep each candidate small and reviewable.
_MAX_CANDIDATE_OPS = 2
ManifestExecutorFactory = Callable[[dict[str, Any]], WorkflowExecutor]


class WorkflowCalibrationError(ValueError):
    """Raised when calibration cannot safely proceed."""


@dataclass(frozen=True)
class WorkflowCalibrationSpec:
    objective: dict[str, Any] = field(
        default_factory=lambda: {"maximize": "quality", "epsilon": 0.02}
    )
    protected: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(
        default_factory=lambda: {"max_candidates": 3, "max_eval_examples": 20, "min_examples": 2}
    )
    dataset: dict[str, Any] = field(
        default_factory=lambda: {"source": "deploy_gate", "dataset_ref": None}
    )
    move_set: list[str] = field(default_factory=list)
    scorers: list[dict[str, Any]] = field(default_factory=list)
    judge: dict[str, Any] = field(default_factory=lambda: {"enabled": False})

    @classmethod
    def from_raw(
        cls, raw: WorkflowCalibrationSpec | dict[str, Any] | None
    ) -> WorkflowCalibrationSpec:
        if isinstance(raw, WorkflowCalibrationSpec):
            return raw
        data = raw or {}
        return cls(
            objective=dict(data.get("objective") or {"maximize": "quality", "epsilon": 0.02}),
            protected=dict(data.get("protected") or {}),
            budget=dict(
                data.get("budget")
                or {"max_candidates": 3, "max_eval_examples": 20, "min_examples": 2}
            ),
            dataset=dict(data.get("dataset") or {"source": "deploy_gate", "dataset_ref": None}),
            move_set=list(data.get("move_set") or []),
            scorers=list(data.get("scorers") or []),
            judge=dict(data.get("judge") or {"enabled": False}),
        )

    @property
    def max_eval_examples(self) -> int:
        return max(1, min(50, int(self.budget.get("max_eval_examples", 20))))

    @property
    def min_examples(self) -> int:
        return max(1, min(self.max_eval_examples, int(self.budget.get("min_examples", 2))))

    @property
    def max_candidates(self) -> int:
        return max(1, min(5, int(self.budget.get("max_candidates", 3))))

    @property
    def dataset_ref(self) -> str | None:
        value = self.dataset.get("dataset_ref")
        return value if isinstance(value, str) and value else None

    @property
    def judge_enabled(self) -> bool:
        return bool(self.judge.get("enabled"))


@dataclass(frozen=True)
class WorkflowCalibrationExample:
    input_text: str
    expected: dict[str, Any]
    weight: float
    tags: list[str]
    example_id: str


@dataclass(frozen=True)
class WorkflowCalibrationRunScore:
    scores: dict[str, float]
    details: dict[str, str]


@dataclass(frozen=True)
class WorkflowCalibrationCandidate:
    candidate_id: str
    patch_kind: str
    semantic_ops: list[dict[str, Any]]
    candidate_manifest: dict[str, Any]
    summary: str
    graph_diff: dict[str, Any]
    prompt_suggestion: str | None = None
    rejected_reason: str | None = None


@dataclass(frozen=True)
class WorkflowCalibrationCandidateResult:
    candidate_id: str
    summary: str
    semantic_ops: list[dict[str, Any]]
    graph_diff: dict[str, Any]
    scores: dict[str, float]
    deltas: dict[str, float]
    accepted: bool
    rejected_reason: str | None
    patch_kind: str
    candidate_manifest: dict[str, Any] | None = None
    prompt_suggestion: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    example_scores: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, include_manifest: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "summary": self.summary,
            "semantic_ops": self.semantic_ops,
            "graph_diff": self.graph_diff,
            "scores": self.scores,
            "deltas": self.deltas,
            "accepted": self.accepted,
            "rejected_reason": self.rejected_reason,
            "patch_kind": self.patch_kind,
            "prompt_suggestion": self.prompt_suggestion,
            "gate": self.gate,
            "example_scores": self.example_scores,
        }
        if include_manifest:
            payload["candidate_manifest"] = self.candidate_manifest
        return payload


@dataclass(frozen=True)
class WorkflowCalibrationResult:
    passed: bool
    baseline_scores: dict[str, float]
    candidates: list[WorkflowCalibrationCandidateResult]
    winner: WorkflowCalibrationCandidateResult | None
    n_examples: int
    low_confidence: bool
    objective: str
    gate: dict[str, Any]
    baseline_example_scores: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self, *, include_manifests: bool = False) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "baseline_scores": self.baseline_scores,
            "candidates": [
                candidate.to_dict(include_manifest=include_manifests)
                for candidate in self.candidates
            ],
            "winner": self.winner.to_dict(include_manifest=include_manifests)
            if self.winner
            else None,
            "n_examples": self.n_examples,
            "low_confidence": self.low_confidence,
            "objective": self.objective,
            "gate": self.gate,
            "baseline_example_scores": self.baseline_example_scores,
            "error": self.error,
        }


def resolve_workflow_calibration_examples(
    session: Session,
    manifest: WorkflowManifest,
    item: CaliberVerificationItem | None,
    spec: WorkflowCalibrationSpec | dict[str, Any] | None,
) -> list[WorkflowCalibrationExample]:
    resolved_spec = WorkflowCalibrationSpec.from_raw(spec)
    dataset_ref, dataset = _resolve_active_dataset(session, manifest, resolved_spec)
    rows = (
        session.execute(
            select(CaliberEvalDatasetExample)
            .where(
                CaliberEvalDatasetExample.dataset_id == dataset.dataset_id,
                CaliberEvalDatasetExample.superseded_at.is_(None),
            )
            .order_by(CaliberEvalDatasetExample.created_at.asc())
            .limit(resolved_spec.max_eval_examples)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise WorkflowCalibrationError(
            f"deploy-gate dataset {dataset_ref!r} has no non-superseded examples"
        )
    examples = [
        WorkflowCalibrationExample(
            input_text=_example_text(row.input),
            expected=dict(row.expected or {}),
            weight=float(row.weight or 1.0),
            tags=list(row.tags or []),
            example_id=row.example_id,
        )
        for row in rows
    ]
    flagged = _flagged_example(item)
    if flagged is not None:
        if resolved_spec.max_eval_examples > 1:
            examples = [flagged, *examples[: resolved_spec.max_eval_examples - 1]]
        else:
            examples = examples[:1]
    return examples


def is_low_confidence_calibration(
    examples: Sequence[WorkflowCalibrationExample],
    spec: WorkflowCalibrationSpec | dict[str, Any] | None,
) -> bool:
    return len(examples) < WorkflowCalibrationSpec.from_raw(spec).min_examples


def score_quality_match(
    result: WorkflowRunResult,
    example: WorkflowCalibrationExample,
) -> dict[str, float]:
    expected = example.expected
    if not expected:
        return {"quality": 0.0}
    expected_value = _expected_output(expected)
    if expected_value is None:
        return {"quality": 0.0}
    if isinstance(expected_value, dict):
        actual = _parse_json_object(result.output)
        score = 1.0 if isinstance(actual, dict) and _is_subset(expected_value, actual) else 0.0
    else:
        score = _score_text_match(
            str(result.output), str(expected_value), _match_kind(expected, example.tags)
        )
    return {"quality": score}


def score_tool_adherence(
    result: WorkflowRunResult,
    example: WorkflowCalibrationExample,
) -> dict[str, float]:
    expected = example.expected
    required = _string_list(expected.get("required_tools") or expected.get("tools_required"))
    forbidden = set(
        _string_list(expected.get("forbidden_tools") or expected.get("tools_forbidden"))
    )
    order = _string_list(expected.get("tool_order"))
    allowed = set(_string_list(expected.get("allowed_tools") or expected.get("tools_allowed")))
    calls = _flatten_tool_calls(result)
    if not required and not forbidden and not order and not allowed:
        return {"tool_adherence": 1.0}
    score = 1.0
    if required:
        present = sum(1 for tool in required if tool in calls)
        score *= present / len(required)
    if forbidden and any(tool in forbidden for tool in calls):
        score = 0.0
    if allowed and any(tool not in allowed for tool in calls):
        score = 0.0
    if order and not _appears_in_order(calls, order):
        score *= 0.5
    return {"tool_adherence": max(0.0, min(1.0, score))}


def score_completion(
    result: WorkflowRunResult, _example: WorkflowCalibrationExample
) -> dict[str, float]:
    return {"completion_rate": 1.0 if result.status == "completed" else 0.0}


def score_safety(
    result: WorkflowRunResult, _example: WorkflowCalibrationExample
) -> dict[str, float]:
    if result.status == "error":
        return {"safety": 0.0}
    passed = all(bool(item.get("passed", True)) for item in result.guardrail_results)
    return {"safety": 1.0 if passed else 0.0}


def score_workflow_calibration_run(
    result: WorkflowRunResult,
    example: WorkflowCalibrationExample,
    spec: WorkflowCalibrationSpec | dict[str, Any] | None = None,
) -> WorkflowCalibrationRunScore:
    """Score one workflow run against one structured calibration example."""
    _ = spec  # reserved for scorer-specific config in later phases
    quality = score_quality_match(result, example)["quality"]
    tool = score_tool_adherence(result, example)["tool_adherence"]
    completion = score_completion(result, example)["completion_rate"]
    safety = score_safety(result, example)["safety"]
    return WorkflowCalibrationRunScore(
        scores={
            "quality": quality,
            "tool_adherence": tool,
            "tool_correctness": tool,
            "completion_rate": completion,
            "safety": safety,
        },
        details={
            "quality": _quality_detail(result, example, quality),
            "tool_adherence": _tool_detail(result, example, tool),
            "completion_rate": "workflow completed" if completion else "workflow did not complete",
            "safety": "all guardrails passed" if safety else "one or more guardrails failed",
        },
    )


def aggregate_weighted_scores(
    per_example_scores: Sequence[dict[str, float]],
    examples: Sequence[WorkflowCalibrationExample],
) -> dict[str, float]:
    if not per_example_scores or not examples:
        return {}
    totals: dict[str, float] = {}
    weights: dict[str, float] = {}
    for scores, example in zip(per_example_scores, examples, strict=False):
        weight = max(0.0, float(example.weight))
        for key, value in scores.items():
            totals[key] = totals.get(key, 0.0) + float(value) * weight
            weights[key] = weights.get(key, 0.0) + weight
    return {key: (totals[key] / weights[key] if weights[key] else 0.0) for key in totals}


def generate_workflow_calibration_candidates(  # noqa: PLR0912
    manifest: WorkflowManifest,
    diagnosis: WorkflowDiagnosis,
    examples: Sequence[WorkflowCalibrationExample],
    spec: WorkflowCalibrationSpec | dict[str, Any] | None,
    *,
    resolver: ToolResolver | None = None,
) -> list[WorkflowCalibrationCandidate]:
    """Generate a small, validated, deduplicated set of calibration candidates."""
    resolved_spec = WorkflowCalibrationSpec.from_raw(spec)
    base = manifest.to_dict()
    raw_ops: list[tuple[str, list[dict[str, Any]], str]] = []

    if _move_enabled(resolved_spec, "add_grounding_guardrail") or _move_enabled(
        resolved_spec, "update_tool_constraint"
    ):
        try:
            seed = generate_workflow_patch(manifest, diagnosis, resolver=resolver)
        except (PatchError, ValueError):
            seed = None
        if seed is not None and seed.patch_kind == "workflow_manifest" and seed.semantic_ops:
            raw_ops.append(("seeded workflow patch", seed.semantic_ops[:2], seed.summary))

    target_agent = _target_agent_id(manifest, diagnosis)
    required_tool = _required_tool_hint(manifest, diagnosis, examples, target_agent)
    if (
        target_agent
        and required_tool
        and COMPONENT_TOOL_CONTRACT in diagnosis.affected_components
        and _move_enabled(resolved_spec, "update_tool_constraint")
    ):
        raw_ops.append(
            (
                "require tool before claims",
                [
                    {
                        "op": "update_tool_constraint",
                        "target_node_id": target_agent,
                        "tool_ref": required_tool,
                        "constraint": "required_before_claim",
                    }
                ],
                f"Require {required_tool!r} before claims in {target_agent!r}.",
            )
        )

    if target_agent and required_tool and _move_enabled(resolved_spec, "add_grounding_guardrail"):
        guard_id = f"{target_agent}_calibration_guard"
        raw_ops.append(
            (
                "add grounding guardrail",
                [
                    {
                        "op": "add_node_after",
                        "target_node_id": target_agent,
                        "edge_id": f"e_{target_agent}_{guard_id}",
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
                                        "tool": required_tool,
                                        "categories": ["refund_policy", "warranty_policy"],
                                    }
                                }
                            ],
                        },
                    }
                ],
                f"Add a grounding guardrail after {target_agent!r}.",
            )
        )

    suggested_handoff = diagnosis.localized_to.get("suggested_handoff")
    if (
        target_agent
        and isinstance(suggested_handoff, str)
        and suggested_handoff
        and COMPONENT_WORKFLOW_EDGE in diagnosis.affected_components
        and _move_enabled(resolved_spec, "reroute_handoff")
    ):
        raw_ops.append(
            (
                "reroute handoff",
                [
                    {
                        "op": "update_node_field",
                        "target_node_id": target_agent,
                        "field_path": "handoffs",
                        "value": [
                            {
                                "target": suggested_handoff,
                                "description": "route per calibration diagnosis",
                            }
                        ],
                    }
                ],
                f"Route {target_agent!r} to hand off to {suggested_handoff!r}.",
            )
        )

    seen_hashes = {compute_manifest_hash(manifest)}
    candidates: list[WorkflowCalibrationCandidate] = []
    for _name, ops, summary in raw_ops:
        if len(candidates) >= resolved_spec.max_candidates:
            break
        if not ops or len(ops) > _MAX_CANDIDATE_OPS:
            continue
        if _weakens_protected_guardrail(manifest, ops):
            continue
        try:
            patched = apply_patch(base, ops, resolver=resolver)
        except (PatchError, ValueError):
            continue
        manifest_hash = compute_manifest_hash(patched)
        if manifest_hash in seen_hashes:
            continue
        report = validate_manifest(patched, resolver=resolver)
        if not report.valid:
            continue
        seen_hashes.add(manifest_hash)
        candidates.append(
            WorkflowCalibrationCandidate(
                candidate_id=f"cal-{len(candidates)}",
                patch_kind="workflow_manifest",
                semantic_ops=ops,
                candidate_manifest=patched.to_dict(),
                summary=summary,
                graph_diff=compute_graph_diff(manifest, patched),
            )
        )
    return candidates


def evaluate_workflow_calibration_candidates(
    baseline_manifest: dict[str, Any],
    candidates: Sequence[WorkflowCalibrationCandidate],
    examples: Sequence[WorkflowCalibrationExample],
    spec: WorkflowCalibrationSpec | dict[str, Any] | None,
    *,
    resolver: ToolResolver,
    baseline_executor: WorkflowExecutor | None = None,
    candidate_executor: WorkflowExecutor | None = None,
    baseline_executor_factory: ManifestExecutorFactory | None = None,
    candidate_executor_factory: ManifestExecutorFactory | None = None,
    scorer: RunScorer | None = None,
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> WorkflowCalibrationResult:
    """Replay and rank bounded workflow calibration candidates.

    When ``scorer`` is supplied (the LLM judge, golden-path Wave 5.2), it
    overrides the structural ``quality`` dimension per run; the other dimensions
    (tool adherence / completion / safety) stay structural. ``None`` → fully
    structural scoring (default, unchanged).
    """
    resolved_spec = WorkflowCalibrationSpec.from_raw(spec)
    objective = _objective_dimension(resolved_spec.objective.get("maximize"))
    if not examples:
        return WorkflowCalibrationResult(
            passed=False,
            baseline_scores={},
            candidates=[],
            winner=None,
            n_examples=0,
            low_confidence=True,
            objective=objective,
            gate={"passed": False, "reasons": ["no calibration examples"]},
            error="no calibration examples",
        )

    low_confidence = is_low_confidence_calibration(examples, resolved_spec)
    try:
        resolved_baseline_executor = _resolve_manifest_executor(
            baseline_manifest,
            executor=baseline_executor,
            executor_factory=baseline_executor_factory,
        )
        baseline_scores, baseline_details = _replay_calibration_manifest(
            baseline_manifest,
            examples,
            resolved_spec,
            resolver=resolver,
            executor=resolved_baseline_executor,
            scorer=scorer,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
        )
    except (CompileError, ValueError) as exc:
        return WorkflowCalibrationResult(
            passed=False,
            baseline_scores={},
            candidates=[],
            winner=None,
            n_examples=len(examples),
            low_confidence=low_confidence,
            objective=objective,
            gate={"passed": False, "reasons": [f"baseline replay failed: {exc}"]},
            error=str(exc),
        )

    results: list[WorkflowCalibrationCandidateResult] = []
    for candidate in candidates:
        try:
            resolved_candidate_executor = _resolve_manifest_executor(
                candidate.candidate_manifest,
                executor=candidate_executor,
                executor_factory=candidate_executor_factory,
            )
            candidate_scores, example_scores = _replay_calibration_manifest(
                candidate.candidate_manifest,
                examples,
                resolved_spec,
                resolver=resolver,
                executor=resolved_candidate_executor,
                scorer=scorer,
                knowledge_query_runner=knowledge_query_runner,
                knowledge_build_runner=knowledge_build_runner,
            )
            deltas = _score_deltas(baseline_scores, candidate_scores)
            accepted, reason, gate = _candidate_gate(
                baseline_manifest,
                candidate_scores,
                deltas,
                resolved_spec,
                objective,
            )
        except (CompileError, ValueError) as exc:
            candidate_scores = {}
            example_scores = []
            deltas = {}
            accepted = False
            reason = f"candidate compile/replay failed: {exc}"
            gate = {"passed": False, "reasons": [reason]}
        results.append(
            WorkflowCalibrationCandidateResult(
                candidate_id=candidate.candidate_id,
                summary=candidate.summary,
                semantic_ops=candidate.semantic_ops,
                graph_diff=candidate.graph_diff,
                scores=candidate_scores,
                deltas=deltas,
                accepted=accepted,
                rejected_reason=None if accepted else reason,
                patch_kind=candidate.patch_kind,
                candidate_manifest=candidate.candidate_manifest,
                prompt_suggestion=candidate.prompt_suggestion,
                gate=gate,
                example_scores=example_scores,
            )
        )

    winner = _select_winner(results, objective)
    if winner is None:
        best = _select_best_near_miss(results, objective)
        reasons = ["no candidate met calibration gates"]
        if best and best.rejected_reason:
            reasons.append(best.rejected_reason)
        return WorkflowCalibrationResult(
            passed=False,
            baseline_scores=baseline_scores,
            candidates=results,
            winner=best,
            n_examples=len(examples),
            low_confidence=low_confidence,
            objective=objective,
            gate={"passed": False, "reasons": reasons},
            baseline_example_scores=baseline_details,
        )

    return WorkflowCalibrationResult(
        passed=True,
        baseline_scores=baseline_scores,
        candidates=results,
        winner=winner,
        n_examples=len(examples),
        low_confidence=low_confidence,
        objective=objective,
        gate={"passed": True, "reasons": [], "low_confidence": low_confidence},
        baseline_example_scores=baseline_details,
    )


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


def _resolve_active_dataset(
    session: Session,
    manifest: WorkflowManifest,
    spec: WorkflowCalibrationSpec,
) -> tuple[str, CaliberEvalDataset]:
    refs = [gate.dataset_ref for gate in manifest.deploy_gates.values()]
    if spec.dataset_ref:
        refs = [spec.dataset_ref]
    for dataset_ref in refs:
        artifact = manifest.artifacts.eval_datasets.get(dataset_ref)
        dataset_name = artifact.dataset_name if artifact else dataset_ref
        dataset = (
            session.execute(
                select(CaliberEvalDataset).where(
                    CaliberEvalDataset.name == dataset_name,
                    CaliberEvalDataset.status == "active",
                )
            )
            .scalars()
            .first()
        )
        if dataset is not None:
            return dataset_ref, dataset
    raise WorkflowCalibrationError("workflow has no active deploy-gate eval dataset")


def _replay_calibration_manifest(
    manifest_dict: dict[str, Any],
    examples: Sequence[WorkflowCalibrationExample],
    spec: WorkflowCalibrationSpec,
    *,
    resolver: ToolResolver,
    executor: WorkflowExecutor,
    scorer: RunScorer | None = None,
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    compiled = compile_workflow(
        parse_manifest(manifest_dict), resolver=resolver, version="calibration"
    )
    if any(getattr(node, "file_ref", None) is not None for node in compiled.ir.nodes.values()):
        raise WorkflowCalibrationError(
            "workflow calibration does not yet support managed file inputs; "
            "run a project-scoped workflow evaluation instead"
        )
    plan = RuntimePlan(
        ir=compiled.ir,
        resolver=resolver,
        knowledge_query_runner=knowledge_query_runner,
        knowledge_build_runner=knowledge_build_runner,
    )
    per_example: list[dict[str, float]] = []
    details: list[dict[str, Any]] = []
    for example in examples:
        run = execute(plan, example.input_text, executor=executor)
        scored = score_workflow_calibration_run(run, example, spec)
        scores = dict(scored.scores)
        if scorer is not None:
            # LLM judge overrides only the quality dimension (it sees the run
            # input + output); structural tool/completion/safety dims stay.
            judged = _invoke_scorer(scorer, run, example.input_text)
            if "quality" in judged:
                scores["quality"] = judged["quality"]
        per_example.append(scores)
        details.append(
            {
                "example_id": example.example_id,
                "scores": scores,
                "details": scored.details,
                "status": run.status,
            }
        )
    return aggregate_weighted_scores(per_example, examples), details


def _flagged_example(item: CaliberVerificationItem | None) -> WorkflowCalibrationExample | None:
    if item is None or not item.free_text:
        return None
    expected = {}
    context = item.submitted_context or {}
    if isinstance(context.get("expected"), dict):
        expected = dict(context["expected"])
    return WorkflowCalibrationExample(
        input_text=item.free_text,
        expected=expected,
        weight=1.0,
        tags=["calibration:flagged"],
        example_id=f"{item.item_id}:flagged",
    )


def _example_text(data: dict[str, Any] | None) -> str:
    data = data or {}
    for key in ("input", "user_message", "message", "query", "text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(data, sort_keys=True)


def _expected_output(expected: dict[str, Any]) -> Any:
    for key in (
        "output",
        "final_output",
        "answer",
        "text",
        "contains",
        "exact",
        "regex",
        "json_subset",
    ):
        if key in expected:
            return expected[key]
    return None


def _quality_detail(
    result: WorkflowRunResult,  # noqa: ARG001
    example: WorkflowCalibrationExample,
    score: float,
) -> str:
    if not example.expected:
        return "expected output is missing"
    expected_value = _expected_output(example.expected)
    if expected_value is None:
        return "expected output is missing"
    if score >= 1.0:
        return "output matched expected value"
    if isinstance(expected_value, dict):
        return "output JSON did not contain expected subset"
    kind = _match_kind(example.expected, example.tags)
    return f"output did not satisfy {kind} match"


def _tool_detail(
    result: WorkflowRunResult,
    example: WorkflowCalibrationExample,
    score: float,
) -> str:
    calls = _flatten_tool_calls(result)
    expected = example.expected
    required = _string_list(expected.get("required_tools") or expected.get("tools_required"))
    forbidden = _string_list(expected.get("forbidden_tools") or expected.get("tools_forbidden"))
    allowed = _string_list(expected.get("allowed_tools") or expected.get("tools_allowed"))
    if not required and not forbidden and not allowed and not expected.get("tool_order"):
        return "no expected tool constraints"
    if score >= 1.0:
        return "tool calls satisfied expected constraints"
    if required and any(tool not in calls for tool in required):
        missing = [tool for tool in required if tool not in calls]
        return f"missing required tool(s): {', '.join(missing)}"
    if forbidden and any(tool in calls for tool in forbidden):
        used = [tool for tool in calls if tool in forbidden]
        return f"used forbidden tool(s): {', '.join(used)}"
    if allowed:
        undeclared = [tool for tool in calls if tool not in allowed]
        if undeclared:
            return f"used undeclared tool(s): {', '.join(undeclared)}"
    return "tool call order did not satisfy expected order"


def _match_kind(expected: dict[str, Any], tags: Sequence[str]) -> str:
    raw = expected.get("match_kind")
    if isinstance(raw, str):
        return raw
    for tag in tags:
        if tag in {"match:exact", "match:contains", "match:regex"}:
            return tag.split(":", 1)[1]
    if "exact" in expected:
        return "exact"
    if "regex" in expected:
        return "regex"
    return "contains"


def _score_text_match(output: str, expected: str, match_kind: str) -> float:
    if match_kind == "exact":
        return 1.0 if output.strip() == expected.strip() else 0.0
    if match_kind == "regex":
        return 1.0 if re.search(expected, output, flags=re.IGNORECASE) else 0.0
    return 1.0 if expected.lower() in output.lower() else 0.0


def _parse_json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_subset(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        actual_value = actual[key]
        if isinstance(value, dict):
            if not isinstance(actual_value, dict) or not _is_subset(value, actual_value):
                return False
        elif actual_value != value:
            return False
    return True


def _flatten_tool_calls(result: WorkflowRunResult) -> list[str]:
    calls: list[str] = []
    for step in result.steps:
        for call in step.tool_calls:
            name = call.get("tool") or call.get("name")
            if isinstance(name, str):
                calls.append(name)
    return calls


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _appears_in_order(calls: Sequence[str], order: Sequence[str]) -> bool:
    position = 0
    for call in calls:
        if position < len(order) and call == order[position]:
            position += 1
    return position == len(order)


def _move_enabled(spec: WorkflowCalibrationSpec, move: str) -> bool:
    return not spec.move_set or move in spec.move_set


def _target_agent_id(
    manifest: WorkflowManifest,
    diagnosis: WorkflowDiagnosis,
) -> str | None:
    node_ids = diagnosis.localized_to.get("node_ids")
    if isinstance(node_ids, list):
        for node_id in node_ids:
            if isinstance(node_id, str) and isinstance(manifest.nodes.get(node_id), AgentNode):
                return node_id
    for node_id, node in manifest.nodes.items():
        if isinstance(node, AgentNode):
            return node_id
    return None


def _required_tool_hint(
    manifest: WorkflowManifest,
    diagnosis: WorkflowDiagnosis,
    examples: Sequence[WorkflowCalibrationExample],
    target_agent: str | None,
) -> str | None:
    tool_refs = diagnosis.localized_to.get("tool_refs")
    if isinstance(tool_refs, list):
        for tool in tool_refs:
            if isinstance(tool, str) and tool:
                return tool
    for example in examples:
        for key in ("required_tools", "tools_required", "tool_order", "allowed_tools"):
            tools = _string_list(example.expected.get(key))
            if tools:
                return tools[0]
    node = manifest.nodes.get(target_agent or "")
    if isinstance(node, AgentNode) and node.tools:
        return node.tools[0]
    return None


def _weakens_protected_guardrail(
    manifest: WorkflowManifest,
    ops: Sequence[dict[str, Any]],
) -> bool:
    protected_failures = {"block", "block_retry", "escalate"}
    for op in ops:
        kind = op.get("op")
        target_id = op.get("target_node_id")
        if not isinstance(target_id, str):
            continue
        node = manifest.nodes.get(target_id)
        if not isinstance(node, GuardrailNode):
            continue
        if kind in {"remove_node", "remove_edge"}:
            return True
        field_path = str(op.get("field_path") or "")
        if (
            kind == "update_node_field"
            and field_path in {"checks", "on_failure", "max_retries"}
            and node.on_failure in protected_failures
        ):
            return True
    return False


def _score_deltas(
    baseline_scores: dict[str, float],
    candidate_scores: dict[str, float],
) -> dict[str, float]:
    keys = set(baseline_scores) | set(candidate_scores)
    return {
        key: candidate_scores.get(key, 0.0) - baseline_scores.get(key, 0.0) for key in sorted(keys)
    }


def _objective_dimension(value: Any) -> str:
    if value == "tool_correctness":
        return "tool_adherence"
    return str(value or "quality")


def _candidate_gate(
    baseline_manifest: dict[str, Any],
    scores: dict[str, float],
    deltas: dict[str, float],
    spec: WorkflowCalibrationSpec,
    objective: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    reasons: list[str] = []
    epsilon = float(spec.objective.get("epsilon", 0.02))
    target_delta = deltas.get(objective, 0.0)
    eps = 1e-9
    if target_delta < epsilon - eps:
        reasons.append(f"{objective} delta {target_delta:+.3f} < epsilon {epsilon}")

    for dim, tolerance in spec.protected.items():
        try:
            allowed_regression = float(tolerance)
        except (TypeError, ValueError):
            allowed_regression = 0.0
        delta = deltas.get(dim, 0.0)
        if delta < -allowed_regression - eps:
            reasons.append(f"{dim} regression {delta:+.3f} exceeds tolerance {allowed_regression}")

    if scores.get("completion_rate", 0.0) < 1.0 - eps:
        reasons.append("completion gate failed")
    if scores.get("safety", 0.0) < 1.0 - eps:
        reasons.append("safety gate failed")

    thresholds = _deploy_gate_thresholds(baseline_manifest)
    min_pass_rate = thresholds.get("min_pass_rate")
    if min_pass_rate is not None and scores.get(objective, 0.0) < min_pass_rate - eps:
        reasons.append(
            f"{objective} score {scores.get(objective, 0.0):.3f} < min_pass_rate {min_pass_rate}"
        )
    min_overall_delta = thresholds.get("min_overall_delta")
    if min_overall_delta is not None and target_delta < min_overall_delta - eps:
        reasons.append(
            f"{objective} delta {target_delta:+.3f} < min_overall_delta {min_overall_delta}"
        )

    passed = not reasons
    return (
        passed,
        None if passed else "; ".join(reasons),
        {
            "passed": passed,
            "reasons": reasons,
            "objective": objective,
            "thresholds": thresholds,
        },
    )


def _deploy_gate_thresholds(manifest_dict: dict[str, Any]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    try:
        manifest = parse_manifest(manifest_dict)
    except ValueError:
        return thresholds
    for gate in manifest.deploy_gates.values():
        for key, value in gate.thresholds.items():
            try:
                thresholds[key] = float(value)
            except (TypeError, ValueError):
                continue
    return thresholds


def _select_winner(
    candidates: Sequence[WorkflowCalibrationCandidateResult],
    objective: str,
) -> WorkflowCalibrationCandidateResult | None:
    accepted = [candidate for candidate in candidates if candidate.accepted]
    if not accepted:
        return None
    return max(accepted, key=lambda candidate: candidate.deltas.get(objective, 0.0))


def _select_best_near_miss(
    candidates: Sequence[WorkflowCalibrationCandidateResult],
    objective: str,
) -> WorkflowCalibrationCandidateResult | None:
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.deltas.get(objective, float("-inf")))


__all__ = [
    "WorkflowCalibrationCandidate",
    "WorkflowCalibrationCandidateResult",
    "WorkflowCalibrationError",
    "WorkflowCalibrationExample",
    "WorkflowCalibrationResult",
    "WorkflowCalibrationRunScore",
    "WorkflowCalibrationSpec",
    "aggregate_weighted_scores",
    "evaluate_workflow_calibration_candidates",
    "generate_workflow_calibration_candidates",
    "is_low_confidence_calibration",
    "resolve_workflow_calibration_examples",
    "score_completion",
    "score_quality_match",
    "score_safety",
    "score_tool_adherence",
    "score_workflow_calibration_run",
]
