"""Unit coverage for workflow-manifest refinement stage helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

import caliber.orchestrator.workflow_stages as stages
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberVerificationItem,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowPatch,
    CaliberWorkflowVersion,
)
from caliber.workflows.calibration import (
    WorkflowCalibrationCandidate,
    WorkflowCalibrationCandidateResult,
    WorkflowCalibrationExample,
    WorkflowCalibrationResult,
)
from caliber.workflows.refinement import (
    WorkflowDiagnosis,
    WorkflowEvalResult,
    WorkflowPatchCandidate,
)
from tests.workflow_helpers import make_manifest, make_support_manifest


def _seed_agent(session: Session) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp-support",
            name="Support",
            owner="@test",
        )
    )


def _seed_workflow(session: Session, workflow_id: str = "wf") -> None:
    session.add(CaliberWorkflow(workflow_id=workflow_id, name="Workflow", owner="@test"))


def _seed_item(
    session: Session,
    *,
    item_id: str = "FB-1",
    workflow_id: str = "wf",
    context: dict[str, object] | None = None,
) -> CaliberVerificationItem:
    item = CaliberVerificationItem(
        item_id=item_id,
        agent_id="support-agent",
        category="tool_use",
        free_text="Agent skipped lookup_policy.",
        severity="standard",
        workflow_id=workflow_id,
        submitted_context=context or {},
    )
    session.add(item)
    return item


def _seed_job(
    session: Session,
    *,
    job_id: str = "RFN-WF",
    workflow_id: str | None = "wf",
    stage: str = "diagnosis",
    status: str = "running",
    diagnosis: dict[str, object] | None = None,
    candidate: dict[str, object] | None = None,
    calibration_spec: dict[str, object] | None = None,
) -> CaliberRefinementJob:
    job = CaliberRefinementJob(
        job_id=job_id,
        agent_id="support-agent",
        primary_item_id="FB-1",
        workflow_id=workflow_id,
        artifact_type="workflow_manifest",
        current_stage=stage,
        status=status,
        diagnosis=diagnosis,
        candidate=candidate,
        calibration_spec=calibration_spec,
    )
    session.add(job)
    return job


def _seed_version(
    session: Session,
    *,
    version_id: str,
    workflow_id: str = "wf",
    number: int,
    status: str = "draft",
    manifest: dict[str, object] | None = None,
) -> CaliberWorkflowVersion:
    version = CaliberWorkflowVersion(
        version_id=version_id,
        workflow_id=workflow_id,
        version_number=number,
        status=status,
        manifest=manifest or make_manifest(workflow_id),
        manifest_hash=f"hash-{version_id}",
    )
    session.add(version)
    return version


def _seed_common(session: Session) -> None:
    _seed_agent(session)
    _seed_workflow(session)
    _seed_item(session)


def test_resolve_baseline_version_precedence_and_errors(db_session: Session) -> None:
    _seed_common(db_session)
    job = _seed_job(db_session, workflow_id=None)
    db_session.commit()

    try:
        stages._resolve_baseline_version(db_session, job, None)
    except stages.WorkflowStageError as exc:
        assert "has no workflow_id" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected WorkflowStageError")

    job.workflow_id = "wf"
    pinned = _seed_version(db_session, version_id="wfv-pinned", number=1, status="draft")
    published = _seed_version(db_session, version_id="wfv-published", number=2, status="published")
    draft_newest = _seed_version(db_session, version_id="wfv-draft", number=3, status="draft")
    item = db_session.get(CaliberVerificationItem, "FB-1")
    assert item is not None
    item.submitted_context = {"workflow_version_id": pinned.version_id}
    db_session.commit()

    assert stages._resolve_baseline_version(db_session, job, item).version_id == "wfv-pinned"

    item.submitted_context = {"workflow_version_id": "missing"}
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-prod",
            workflow_id="wf",
            alias="prod",
            version_id=published.version_id,
        )
    )
    db_session.commit()
    assert stages._resolve_baseline_version(db_session, job, item).version_id == "wfv-published"

    db_session.query(CaliberWorkflowDeployment).delete()
    db_session.commit()
    assert stages._resolve_baseline_version(db_session, job, item).version_id == "wfv-published"

    published.status = "deprecated"
    db_session.commit()
    assert (
        stages._resolve_baseline_version(db_session, job, item).version_id
        == draft_newest.version_id
    )

    db_session.query(CaliberWorkflowDeployment).delete()
    db_session.query(CaliberWorkflowVersion).delete()
    db_session.commit()
    try:
        stages._resolve_baseline_version(db_session, job, item)
    except stages.WorkflowStageError as exc:
        assert "has no versions" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected WorkflowStageError")


def test_target_alias_prefers_staging_then_dev_then_default(db_session: Session) -> None:
    _seed_workflow(db_session)
    _seed_version(db_session, version_id="wfv-1", number=1)
    db_session.commit()

    assert stages._target_alias(db_session, "wf") == "dev"

    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-prod",
            workflow_id="wf",
            alias="prod",
            version_id="wfv-1",
        )
    )
    db_session.commit()
    assert stages._target_alias(db_session, "wf") == "dev"

    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-dev",
            workflow_id="wf",
            alias="dev",
            version_id="wfv-1",
        )
    )
    db_session.commit()
    assert stages._target_alias(db_session, "wf") == "dev"

    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-staging",
            workflow_id="wf",
            alias="staging",
            version_id="wfv-1",
        )
    )
    db_session.commit()
    assert stages._target_alias(db_session, "wf") == "staging"


def test_evidence_and_dataset_helpers(db_session: Session) -> None:
    _seed_common(db_session)
    db_session.flush()
    item = db_session.get(CaliberVerificationItem, "FB-1")
    job = db_session.get(CaliberRefinementJob, "missing") or _seed_job(db_session)
    assert item is not None
    item.submitted_context = {
        "node_id": "agent",
        "edge_id": "e1",
        "workflow_version_id": "wfv-1",
        "required_tools": ["lookup_policy"],
        "observed_tool_calls": [],
        "ignored": "not copied",
    }
    evidence = stages._evidence_from_item(job, item)
    assert evidence["node_id"] == "agent"
    assert "ignored" not in evidence
    assert stages._evidence_from_item(job, None)["free_text"] == ""

    dataset = CaliberEvalDataset(
        dataset_id="ds-1",
        name="support-eval-v3",
        owner="@test",
        version=1,
        status="active",
    )
    db_session.add(dataset)
    db_session.add_all(
        [
            CaliberEvalDatasetExample(
                example_id="ex-1",
                dataset_id="ds-1",
                dataset_version=1,
                input={"message": "from dataset"},
                expected={},
            ),
            CaliberEvalDatasetExample(
                example_id="ex-2",
                dataset_id="ds-1",
                dataset_version=1,
                input={"input": "retired"},
                expected={},
                superseded_at=__import__("datetime").datetime.now(),
            ),
        ]
    )
    db_session.commit()

    manifest = stages.parse_manifest(
        make_support_manifest(
            "wf",
            deploy_gates={
                "support_eval": {
                    "type": "deploy_gate",
                    "dataset_ref": "support_eval",
                    "required_for_aliases": ["dev"],
                    "thresholds": {},
                }
            },
        )
    )
    assert stages._dataset_inputs(db_session, manifest, item) == ["from dataset"]

    item.free_text = "flagged text"
    manifest_without_dataset = stages.parse_manifest(make_manifest("wf"))
    assert stages._dataset_inputs(db_session, manifest_without_dataset, item) == ["flagged text"]
    assert stages._dataset_inputs(db_session, manifest_without_dataset, None) == [
        "Replay the flagged scenario."
    ]
    assert stages._example_text({"query": "search"}) == "search"
    assert stages._example_text({"nested": {"x": 1}}) == '{"nested": {"x": 1}}'


def test_run_workflow_diagnosis_advances_stage(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    job = _seed_job(db_session)
    db_session.commit()

    def _localize(evidence: dict[str, object]) -> WorkflowDiagnosis:
        assert evidence["node_id"] if "node_id" in evidence else True
        return WorkflowDiagnosis(
            root_cause="Missing tool grounding.",
            affected_components=["tool_contract"],
            localized_to={"node_ids": ["agent"], "tool_refs": ["lookup_policy"]},
            recommended_patch_type="workflow_manifest",
            confidence=0.9,
        )

    monkeypatch.setattr(stages, "localize_failure", _localize)
    updated = stages.run_workflow_diagnosis(db_session, job, actor="@tester")

    assert updated.current_stage == "candidate"
    assert updated.diagnosis["workflow"] is True
    assert db_session.query(CaliberAuditLog).filter_by(actor="@tester").count() == 1


def test_run_workflow_candidate_builds_patch_record(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    version = _seed_version(
        db_session, version_id="wfv-1", number=1, status="published", manifest=base
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-dev",
            workflow_id="wf",
            alias="dev",
            version_id=version.version_id,
        )
    )
    job = _seed_job(
        db_session,
        stage="candidate",
        diagnosis={
            "root_cause": "Missing guardrail.",
            "affected_components": ["guardrail"],
            "localized_to": {"node_ids": ["agent"]},
            "recommended_patch_type": "workflow_manifest",
            "confidence": 0.8,
        },
    )
    db_session.commit()

    candidate = WorkflowPatchCandidate(
        patch_kind="workflow_manifest",
        semantic_ops=[{"op": "update_node_field"}],
        candidate_manifest=base,
        summary="patched",
        graph_diff={"added_nodes": []},
    )
    monkeypatch.setattr(stages, "generate_workflow_patch", lambda *args, **kwargs: candidate)

    updated = stages.run_workflow_candidate(db_session, job, actor="@tester")

    assert updated.current_stage == "eval"
    assert updated.candidate["base_version_id"] == "wfv-1"
    assert updated.candidate["target_alias"] == "dev"
    assert db_session.query(CaliberWorkflowPatch).filter_by(job_id=job.job_id).count() == 1


def test_run_workflow_candidate_requires_diagnosis(db_session: Session) -> None:
    _seed_common(db_session)
    job = _seed_job(db_session, stage="candidate")
    db_session.commit()

    try:
        stages.run_workflow_candidate(db_session, job)
    except stages.WorkflowStageError as exc:
        assert "has no diagnosis" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected WorkflowStageError")


def test_run_workflow_eval_rejects_failed_gate(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    job = _seed_job(
        db_session,
        stage="eval",
        diagnosis={"root_cause": "x"},
        candidate={"candidate_manifest": base, "baseline_manifest": base},
    )
    db_session.commit()
    monkeypatch.setattr(
        stages,
        "evaluate_candidate",
        lambda *args, **kwargs: WorkflowEvalResult(
            passed=False,
            baseline_scores={"quality": 0.8},
            candidate_scores={"quality": 0.7},
            deltas={"quality": -0.1},
            n_examples=1,
            gate={"passed": False, "reasons": ["quality dropped"]},
        ),
    )

    updated = stages.run_workflow_eval(db_session, job, actor="@tester")

    assert updated.status == "rejected"
    assert updated.current_stage == "done"
    assert "quality dropped" in (updated.error_message or "")
    assert db_session.query(CaliberApprovalRequest).count() == 0


def test_run_workflow_eval_marks_candidate_ready_on_pass(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    job = _seed_job(
        db_session,
        stage="eval",
        diagnosis={"root_cause": "x"},
        candidate={"candidate_manifest": base, "baseline_manifest": base},
    )
    db_session.commit()
    monkeypatch.setattr(
        stages,
        "evaluate_candidate",
        lambda *args, **kwargs: WorkflowEvalResult(
            passed=True,
            baseline_scores={"quality": 0.7},
            candidate_scores={"quality": 0.9},
            deltas={"quality": 0.2},
            n_examples=1,
            gate={"passed": True},
        ),
    )

    updated = stages.run_workflow_eval(db_session, job, actor="@tester")

    # Approval governance removed: a passing workflow gate lands the job at
    # the terminal ``candidate_ready/done`` state with no approval row.
    assert updated.status == "candidate_ready"
    assert updated.current_stage == "done"
    assert db_session.query(CaliberApprovalRequest).count() == 0
    assert updated.candidate == {"candidate_manifest": base, "baseline_manifest": base}


def test_run_workflow_eval_forwards_manifest_aware_executor_factories(
    db_session: Session,
    monkeypatch,
) -> None:
    """Workflow eval resolves executors per manifest, not just once globally."""
    _seed_common(db_session)
    base = make_manifest("wf")
    candidate_manifest = make_manifest("wf")
    candidate_manifest["runtime"]["openai"] = {"workflow_api": "responses"}
    job = _seed_job(
        db_session,
        stage="eval",
        diagnosis={"root_cause": "x"},
        candidate={"candidate_manifest": candidate_manifest, "baseline_manifest": base},
    )
    db_session.commit()

    baseline_executor = object()
    candidate_executor = object()
    seen: dict[str, object] = {}

    def _fake_build_executor(cfg, **_kwargs):
        seen["config"] = cfg
        seen.setdefault("build_executor_kwargs", []).append(_kwargs)
        manifest = _kwargs["manifest"]
        runtime_openai = getattr(getattr(manifest, "runtime", None), "openai", None)
        if getattr(runtime_openai, "workflow_api", None) == "responses":
            return candidate_executor
        return baseline_executor

    def _fake_evaluate(baseline_manifest, candidate_manifest, *args, **kwargs):
        seen["baseline_executor"] = kwargs["baseline_executor_factory"](baseline_manifest)
        seen["candidate_executor"] = kwargs["candidate_executor_factory"](candidate_manifest)
        seen["scorer"] = kwargs.get("scorer")
        return WorkflowEvalResult(
            passed=True,
            baseline_scores={"quality": 0.7},
            candidate_scores={"quality": 0.9},
            deltas={"quality": 0.2},
            n_examples=1,
            gate={"passed": True},
        )

    sentinel_scorer = object()
    monkeypatch.setattr(stages, "build_executor", _fake_build_executor)
    monkeypatch.setattr(stages, "build_llm_judge_scorer", lambda cfg: sentinel_scorer)
    monkeypatch.setattr(stages, "evaluate_candidate", _fake_evaluate)

    my_config = object()
    stages.run_workflow_eval(db_session, job, actor="@tester", config=my_config)

    assert seen["config"] is my_config
    build_kwargs = seen["build_executor_kwargs"]
    assert isinstance(build_kwargs, list)
    assert len(build_kwargs) == 2
    assert all(entry["manifest"].workflow_id == "wf" for entry in build_kwargs)
    assert seen["baseline_executor"] is baseline_executor
    assert seen["candidate_executor"] is candidate_executor
    assert seen["scorer"] is sentinel_scorer


def test_run_workflow_eval_validates_candidate_payload(db_session: Session) -> None:
    _seed_common(db_session)
    job = _seed_job(db_session, stage="eval", diagnosis={"root_cause": "x"})
    db_session.commit()

    try:
        stages.run_workflow_eval(db_session, job)
    except stages.WorkflowStageError as exc:
        assert "has no candidate" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected WorkflowStageError")


def test_run_workflow_calibration_candidate_selects_winner_patch_only(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    version = _seed_version(
        db_session, version_id="wfv-1", number=1, status="published", manifest=base
    )
    job = _seed_job(
        db_session,
        stage="candidate",
        diagnosis={
            "root_cause": "Missing tool.",
            "affected_components": ["tool_contract"],
            "localized_to": {"node_ids": ["agent"], "tool_refs": ["lookup_policy"]},
            "recommended_patch_type": "workflow_manifest",
            "confidence": 0.8,
        },
        calibration_spec={
            "objective": {"maximize": "quality", "epsilon": 0.1},
            "budget": {"max_candidates": 2, "max_eval_examples": 20, "min_examples": 1},
        },
    )
    db_session.commit()
    candidate_manifest = make_manifest("wf")
    candidate_manifest["nodes"]["agent"]["instructions"]["text"] = "better"
    winner = WorkflowCalibrationCandidateResult(
        candidate_id="cal-1",
        summary="winner",
        semantic_ops=[{"op": "update_tool_constraint"}],
        graph_diff={"changed_nodes": ["agent"]},
        scores={"quality": 1.0, "completion_rate": 1.0, "safety": 1.0},
        deltas={"quality": 0.2},
        accepted=True,
        rejected_reason=None,
        patch_kind="workflow_manifest",
        candidate_manifest=candidate_manifest,
        gate={"passed": True, "reasons": []},
    )
    near_miss = WorkflowCalibrationCandidateResult(
        candidate_id="cal-0",
        summary="near miss",
        semantic_ops=[{"op": "add_node_after"}],
        graph_diff={},
        scores={"quality": 0.8, "completion_rate": 1.0, "safety": 1.0},
        deltas={"quality": 0.05},
        accepted=False,
        rejected_reason="quality delta too small",
        patch_kind="workflow_manifest",
        candidate_manifest=base,
        gate={"passed": False, "reasons": ["quality delta too small"]},
    )
    monkeypatch.setattr(
        stages,
        "resolve_workflow_calibration_examples",
        lambda *args, **kwargs: [
            WorkflowCalibrationExample(
                input_text="case",
                expected={"contains": "better"},
                weight=1.0,
                tags=[],
                example_id="ex-1",
            )
        ],
    )
    monkeypatch.setattr(
        stages,
        "generate_workflow_calibration_candidates",
        lambda *args, **kwargs: [
            WorkflowCalibrationCandidate(
                candidate_id="cal-0",
                patch_kind="workflow_manifest",
                semantic_ops=near_miss.semantic_ops,
                candidate_manifest=base,
                summary="near miss",
                graph_diff={},
            ),
            WorkflowCalibrationCandidate(
                candidate_id="cal-1",
                patch_kind="workflow_manifest",
                semantic_ops=winner.semantic_ops,
                candidate_manifest=candidate_manifest,
                summary="winner",
                graph_diff=winner.graph_diff,
            ),
        ],
    )
    monkeypatch.setattr(
        stages,
        "evaluate_workflow_calibration_candidates",
        lambda *args, **kwargs: WorkflowCalibrationResult(
            passed=True,
            baseline_scores={"quality": 0.8},
            candidates=[near_miss, winner],
            winner=winner,
            n_examples=1,
            low_confidence=False,
            objective="quality",
            gate={"passed": True, "reasons": []},
        ),
    )

    updated = stages.run_workflow_candidate(db_session, job, actor="@tester")

    assert updated.current_stage == "eval"
    assert updated.candidate["calibration"] is True
    assert updated.candidate["calibration_winner_id"] == "cal-1"
    assert updated.candidate["base_version_id"] == version.version_id
    assert db_session.query(CaliberWorkflowPatch).filter_by(job_id=job.job_id).count() == 1


@pytest.mark.parametrize("judge_enabled", [False, True])
def test_run_workflow_calibration_candidate_respects_judge_flag(
    db_session: Session,
    monkeypatch,
    judge_enabled: bool,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    _seed_version(db_session, version_id="wfv-1", number=1, status="published", manifest=base)
    job = _seed_job(
        db_session,
        stage="candidate",
        diagnosis={
            "root_cause": "Missing tool.",
            "affected_components": ["tool_contract"],
            "localized_to": {"node_ids": ["agent"], "tool_refs": ["lookup_policy"]},
            "recommended_patch_type": "workflow_manifest",
            "confidence": 0.8,
        },
        calibration_spec={
            "objective": {"maximize": "quality", "epsilon": 0.1},
            "budget": {"max_candidates": 1, "max_eval_examples": 5, "min_examples": 1},
            "judge": {"enabled": judge_enabled},
        },
    )
    db_session.commit()
    candidate_manifest = make_manifest("wf")
    candidate_manifest["nodes"]["agent"]["instructions"]["text"] = "better"
    candidate_manifest["runtime"]["openai"] = {"workflow_api": "responses"}
    baseline_executor = object()
    candidate_executor = object()
    sentinel_scorer = object()
    seen: dict[str, object] = {"build_scorer_calls": 0}

    def _fake_build_executor(cfg, **_kwargs):
        manifest = _kwargs["manifest"]
        runtime_openai = getattr(getattr(manifest, "runtime", None), "openai", None)
        if getattr(runtime_openai, "workflow_api", None) == "responses":
            return candidate_executor
        return baseline_executor

    monkeypatch.setattr(stages, "build_executor", _fake_build_executor)
    monkeypatch.setattr(
        stages,
        "resolve_workflow_calibration_examples",
        lambda *args, **kwargs: [
            WorkflowCalibrationExample(
                input_text="case",
                expected={"contains": "better"},
                weight=1.0,
                tags=[],
                example_id="ex-1",
            )
        ],
    )
    monkeypatch.setattr(
        stages,
        "generate_workflow_calibration_candidates",
        lambda *args, **kwargs: [
            WorkflowCalibrationCandidate(
                candidate_id="cal-1",
                patch_kind="workflow_manifest",
                semantic_ops=[{"op": "update_tool_constraint"}],
                candidate_manifest=candidate_manifest,
                summary="winner",
                graph_diff={"changed_nodes": ["agent"]},
            )
        ],
    )

    def _fake_build_scorer(cfg):
        seen["build_scorer_calls"] = int(seen["build_scorer_calls"]) + 1
        return sentinel_scorer

    def _fake_evaluate(baseline_manifest, candidates, *args, **kwargs):
        seen["baseline_executor"] = kwargs["baseline_executor_factory"](baseline_manifest)
        seen["candidate_executor"] = kwargs["candidate_executor_factory"](
            candidates[0].candidate_manifest
        )
        seen["scorer"] = kwargs.get("scorer")
        winner = WorkflowCalibrationCandidateResult(
            candidate_id="cal-1",
            summary="winner",
            semantic_ops=[{"op": "update_tool_constraint"}],
            graph_diff={"changed_nodes": ["agent"]},
            scores={"quality": 1.0},
            deltas={"quality": 0.2},
            accepted=True,
            rejected_reason=None,
            patch_kind="workflow_manifest",
            candidate_manifest=candidate_manifest,
            gate={"passed": True, "reasons": []},
        )
        return WorkflowCalibrationResult(
            passed=True,
            baseline_scores={"quality": 0.8},
            candidates=[winner],
            winner=winner,
            n_examples=1,
            low_confidence=False,
            objective="quality",
            gate={"passed": True, "reasons": []},
        )

    monkeypatch.setattr(stages, "build_llm_judge_scorer", _fake_build_scorer)
    monkeypatch.setattr(stages, "evaluate_workflow_calibration_candidates", _fake_evaluate)

    stages.run_workflow_candidate(
        db_session,
        job,
        actor="@tester",
        config=SimpleNamespace(),
    )

    assert seen["baseline_executor"] is baseline_executor
    assert seen["candidate_executor"] is candidate_executor
    assert seen["scorer"] is (sentinel_scorer if judge_enabled else None)
    assert seen["build_scorer_calls"] == (1 if judge_enabled else 0)


def test_run_workflow_calibration_candidate_rejects_when_no_candidate_passes(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    _seed_version(db_session, version_id="wfv-1", number=1, status="published", manifest=base)
    job = _seed_job(
        db_session,
        stage="candidate",
        diagnosis={"root_cause": "x", "affected_components": [], "localized_to": {}},
        calibration_spec={"objective": {"maximize": "quality", "epsilon": 0.1}},
    )
    db_session.commit()
    near_miss = WorkflowCalibrationCandidateResult(
        candidate_id="cal-0",
        summary="near miss",
        semantic_ops=[],
        graph_diff={},
        scores={"quality": 0.5},
        deltas={"quality": 0.0},
        accepted=False,
        rejected_reason="quality delta too small",
        patch_kind="workflow_manifest",
        candidate_manifest=base,
        gate={"passed": False, "reasons": ["quality delta too small"]},
    )
    monkeypatch.setattr(stages, "resolve_workflow_calibration_examples", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        stages, "generate_workflow_calibration_candidates", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        stages,
        "evaluate_workflow_calibration_candidates",
        lambda *args, **kwargs: WorkflowCalibrationResult(
            passed=False,
            baseline_scores={"quality": 0.5},
            candidates=[near_miss],
            winner=near_miss,
            n_examples=1,
            low_confidence=True,
            objective="quality",
            gate={"passed": False, "reasons": ["no candidate met calibration gates"]},
        ),
    )

    updated = stages.run_workflow_candidate(db_session, job, actor="@tester")

    assert updated.status == "rejected"
    assert updated.current_stage == "done"
    assert updated.eval_results["calibration"] is True
    assert db_session.query(CaliberWorkflowPatch).filter_by(job_id=job.job_id).count() == 0


def test_run_workflow_calibration_eval_reuses_selected_winner_scores(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    job = _seed_job(
        db_session,
        stage="eval",
        diagnosis={"root_cause": "x"},
        calibration_spec={"objective": {"maximize": "quality", "epsilon": 0.1}},
        candidate={
            "artifact_type": "workflow_manifest",
            "candidate_manifest": base,
            "baseline_manifest": base,
            "calibration": True,
            "calibration_winner_id": "cal-1",
            "calibration_patch_id": "WFP-1",
            "calibration_baseline_scores": {"quality": 0.7},
            "calibration_n_examples": 2,
            "calibration_low_confidence": False,
            "calibration_gate": {"passed": True, "reasons": []},
            "calibration_candidates": [
                {
                    "candidate_id": "cal-0",
                    "accepted": False,
                    "rejected_reason": "near miss",
                    "scores": {"quality": 0.72},
                    "deltas": {"quality": 0.02},
                    "semantic_ops": [],
                    "graph_diff": {},
                    "summary": "near miss",
                    "patch_kind": "workflow_manifest",
                },
                {
                    "candidate_id": "cal-1",
                    "accepted": True,
                    "scores": {"quality": 0.9, "completion_rate": 1.0, "safety": 1.0},
                    "deltas": {"quality": 0.2},
                    "semantic_ops": [],
                    "graph_diff": {},
                    "summary": "winner",
                    "patch_kind": "workflow_manifest",
                },
            ],
        },
    )
    db_session.commit()

    def _unexpected_replay(*args, **kwargs):
        raise AssertionError("calibration eval should reuse selected winner scores")

    monkeypatch.setattr(stages, "evaluate_candidate", _unexpected_replay)

    updated = stages.run_workflow_eval(db_session, job, actor="@tester")

    assert updated.status == "candidate_ready"
    assert updated.current_stage == "done"
    assert updated.eval_results["calibration"] is True
    assert db_session.query(CaliberApprovalRequest).count() == 0
    assert updated.candidate["calibration_winner_id"] == "cal-1"

    job.candidate = {"candidate_manifest": {}, "baseline_manifest": "not a dict"}
    db_session.commit()
    try:
        stages.run_workflow_eval(db_session, job)
    except stages.WorkflowStageError as exc:
        assert "candidate is missing manifests" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected WorkflowStageError")
