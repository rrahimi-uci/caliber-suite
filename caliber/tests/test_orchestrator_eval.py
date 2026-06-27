"""Tests for the eval stage of the refinement pipeline."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.artifact_store import FakeArtifactStore
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberRegressionRun,
    CaliberVerificationItem,
)
from caliber.eval.fake import FakeEvalProvider
from caliber.eval.provider import EvalComparison, EvalProviderError, EvalRequest, ScoreSet
from caliber.orchestrator.eval_stage import EvalStateError, run_eval
from caliber.regression import candidate_hash


def _seed_job(
    session: Session,
    *,
    status: str = "running",
    stage: str = "eval",
    candidate: dict[str, object] | None = None,
    eval_thresholds: dict[str, object] | None = None,
    submitted_context: dict[str, object] | None = None,
) -> CaliberRefinementJob:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds=eval_thresholds or {},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-E",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
            submitted_context=submitted_context,
        )
    )
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-E",
        agent_id="support-agent",
        primary_item_id="FB-E",
        artifact_type="prompt",
        status=status,
        current_stage=stage,
        bundle_targets=[],
        diagnosis={"root_cause": "x", "confidence": 0.8, "alternatives": []},
        candidate=candidate
        or {
            "artifact_type": "prompt",
            "content": "rewritten prompt body",
            "rationale": "addresses tool skip",
            "diff_summary": "+5 / -3 lines",
        },
    )
    session.add(job)
    session.commit()
    return job


def test_run_eval_passing_marks_candidate_ready(db_session: Session) -> None:
    _seed_job(db_session)
    job = run_eval(db_session, "RFN-E", FakeEvalProvider())

    # Human-feedback approval governance was removed: a passing gate lands the
    # job at the terminal ``candidate_ready/done`` state.
    assert job.status == "candidate_ready"
    assert job.current_stage == "done"
    assert job.eval_results is not None
    assert job.eval_results["gate"]["passed"] is True
    # The candidate that cleared the gate stays on the job for the operator
    # to promote via the Apply endpoint.
    assert job.candidate is not None
    assert job.candidate["content"] == "rewritten prompt body"


def test_run_eval_passing_creates_no_approval(db_session: Session) -> None:
    _seed_job(db_session)
    run_eval(db_session, "RFN-E", FakeEvalProvider())

    approvals = (
        db_session.execute(
            select(CaliberApprovalRequest).where(CaliberApprovalRequest.job_id == "RFN-E")
        )
        .scalars()
        .all()
    )
    assert approvals == []


def test_run_eval_passing_creates_regression_run_without_approval(db_session: Session) -> None:
    _seed_job(
        db_session,
        eval_thresholds={"eval_dataset_id": "golden-support"},
    )
    run_eval(db_session, "RFN-E", FakeEvalProvider())

    runs = db_session.execute(select(CaliberRegressionRun)).scalars().all()
    assert len(runs) == 1
    run = runs[0]
    # Regression rows are now provenance-only — no approval is created.
    assert run.approval_id is None
    assert run.job_id == "RFN-E"
    assert run.agent_id == "support-agent"
    assert run.status == "passed"
    assert run.required_for_approval is True
    assert run.candidate_hash == candidate_hash("rewritten prompt body")
    assert run.dataset_ids == ["golden-support"]
    assert run.candidate_scores["overall"] == 0.94  # type: ignore[index]


def test_run_eval_replays_against_captured_baseline_content(db_session: Session) -> None:
    _seed_job(
        db_session,
        candidate={
            "artifact_type": "prompt",
            "content": "rewritten prompt body",
            "rationale": "addresses tool skip",
            "diff_summary": "+5 / -3 lines",
            "baseline_content": "production prompt before candidate",
        },
    )
    provider = FakeEvalProvider()
    run_eval(db_session, "RFN-E", provider)

    assert provider.calls[0].baseline_content == "production prompt before candidate"
    run = db_session.execute(select(CaliberRegressionRun)).scalar_one()
    assert run.baseline_scores is not None


def test_run_eval_prefers_live_artifact_store_over_captured_baseline(
    db_session: Session,
) -> None:
    _seed_job(
        db_session,
        candidate={
            "artifact_type": "prompt",
            "content": "rewritten prompt body",
            "rationale": "addresses tool skip",
            "diff_summary": "+5 / -3 lines",
            "baseline_content": "older captured prompt",
        },
    )
    provider = FakeEvalProvider()
    store = FakeArtifactStore({"support-agent": "currently deployed prompt"})
    run_eval(db_session, "RFN-E", provider, artifact_store=store)

    assert provider.calls[0].baseline_content == "currently deployed prompt"


def test_run_eval_passing_writes_two_audit_rows(db_session: Session) -> None:
    """Terminal candidate_ready transition + persisted replay record."""
    _seed_job(db_session)
    run_eval(db_session, "RFN-E", FakeEvalProvider(), actor="@reza")

    rows = (
        db_session.execute(
            select(CaliberAuditLog).where(
                (CaliberAuditLog.entity_id == "RFN-E")
                | (CaliberAuditLog.entity_type == "regression_run")
            )
        )
        .scalars()
        .all()
    )
    actions = sorted(r.action for r in rows)
    assert actions == ["candidate_ready", "record_regression_replay"]


def test_run_eval_rejecting_marks_job_rejected(db_session: Session) -> None:
    """Regression on a per-dim score beyond tolerance fails the gate."""
    _seed_job(db_session)
    provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.5, dimensions={"factual": 0.5, "tone": 0.5}),
        baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88, "tone": 0.89}),
    )
    job = run_eval(db_session, "RFN-E", provider)

    assert job.status == "rejected"
    assert job.current_stage == "done"
    assert job.error_message is not None
    assert "regression gate failed" in job.error_message


def test_run_eval_rejection_does_not_create_approval(db_session: Session) -> None:
    _seed_job(db_session)
    provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.5, dimensions={"factual": 0.5}),
        baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88}),
    )
    run_eval(db_session, "RFN-E", provider)

    approvals = db_session.execute(select(CaliberApprovalRequest)).scalars().all()
    assert approvals == []


def test_run_eval_rejection_persists_failed_regression_run(db_session: Session) -> None:
    _seed_job(db_session)
    provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.5, dimensions={"factual": 0.5}),
        baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88}),
    )
    run_eval(db_session, "RFN-E", provider)

    runs = db_session.execute(select(CaliberRegressionRun)).scalars().all()
    assert len(runs) == 1
    run = runs[0]
    assert run.approval_id is None
    assert run.status == "failed"
    assert run.failure_reason
    assert run.regressions


def test_run_eval_rejection_writes_reject_by_gate_audit_row(db_session: Session) -> None:
    _seed_job(db_session)
    provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.5, dimensions={"factual": 0.5}),
        baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88}),
    )
    run_eval(db_session, "RFN-E", provider)

    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-E"))
        .scalars()
        .all()
    )
    actions = {row.action for row in rows}
    assert actions == {"reject_by_gate"}
    replay_rows = (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.entity_type == "regression_run")
        )
        .scalars()
        .all()
    )
    assert len(replay_rows) == 1
    assert replay_rows[0].action == "record_regression_replay"


# ───────────────────── iterative candidate loop (refinement_max_iterations) ─────


def _failing_provider() -> FakeEvalProvider:
    return FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.5, dimensions={"factual": 0.5, "tone": 0.5}),
        baseline_scores=ScoreSet(overall=0.88, dimensions={"factual": 0.88, "tone": 0.89}),
    )


def test_run_eval_loops_back_to_candidate_when_iterations_remain(
    db_session: Session, app_config: CaliberConfig
) -> None:
    _seed_job(db_session)
    cfg = app_config.model_copy(update={"refinement_max_iterations": 2})

    job = run_eval(db_session, "RFN-E", _failing_provider(), config=cfg)

    # Failed gate, but an iteration remains → loop back to candidate, not reject.
    assert job.status == "running"
    assert job.current_stage == "candidate"
    assert job.refine_iteration == 1
    # The gate feedback is fed to the candidate stage via review_notes.
    assert job.review_notes is not None
    assert "regression gate" in job.review_notes.lower()
    # No approval is created on a retry.
    assert db_session.execute(select(CaliberApprovalRequest)).scalars().all() == []


def test_run_eval_retry_writes_refine_retry_audit(
    db_session: Session, app_config: CaliberConfig
) -> None:
    _seed_job(db_session)
    cfg = app_config.model_copy(update={"refinement_max_iterations": 1})

    run_eval(db_session, "RFN-E", _failing_provider(), config=cfg)

    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == "RFN-E"))
        .scalars()
        .all()
    )
    assert {r.action for r in rows} == {"refine_retry"}


def test_run_eval_rejects_after_iterations_exhausted(
    db_session: Session, app_config: CaliberConfig
) -> None:
    job = _seed_job(db_session)
    job.refine_iteration = 1  # already used the single allowed iteration
    db_session.commit()
    cfg = app_config.model_copy(update={"refinement_max_iterations": 1})

    job = run_eval(db_session, "RFN-E", _failing_provider(), config=cfg)

    assert job.status == "rejected"
    assert job.current_stage == "done"
    assert "regression gate failed" in (job.error_message or "")


def test_run_eval_default_config_rejects_without_looping(db_session: Session) -> None:
    """Default (no config → max_iterations=0) keeps the terminal-reject behavior."""
    _seed_job(db_session)

    job = run_eval(db_session, "RFN-E", _failing_provider())  # no config

    assert job.status == "rejected"
    assert job.refine_iteration == 0


def test_run_eval_honors_custom_thresholds(db_session: Session) -> None:
    """Agent thresholds in eval_thresholds override the gate defaults."""
    _seed_job(
        db_session,
        eval_thresholds={"min_aggregate_score": 0.50, "max_regression_delta": 0.20},
    )
    # Even with poor candidate, looser thresholds pass.
    provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.55, dimensions={"factual": 0.55}),
        baseline_scores=ScoreSet(overall=0.65, dimensions={"factual": 0.65}),
    )
    job = run_eval(db_session, "RFN-E", provider)
    assert job.status == "candidate_ready"


def test_run_eval_honors_prompt_optimization_eval_overrides(db_session: Session) -> None:
    """Manual prompt-optimization context can override dataset, scorer weights, and gate."""
    _seed_job(
        db_session,
        eval_thresholds={"min_aggregate_score": 0.95, "max_regression_delta": 0.02},
        submitted_context={
            "source": "prompt_optimization",
            "prompt_optimization": {
                "eval_dataset_id": "EDS-prompt-opt",
                "scorers": [
                    {"name": "factual", "weight": 0.2, "config": {}},
                    {"name": "tone", "weight": 0.8, "config": {}},
                ],
                "gate": {"min_aggregate_score": 0.8, "max_regression_delta": 0.2},
            },
        },
    )
    provider = FakeEvalProvider(
        candidate_scores=ScoreSet(overall=0.51, dimensions={"factual": 0.9, "tone": 0.8}),
        baseline_scores=ScoreSet(overall=0.97, dimensions={"factual": 0.8, "tone": 0.75}),
    )

    job = run_eval(db_session, "RFN-E", provider)

    assert provider.calls[0].eval_dataset_id == "EDS-prompt-opt"
    assert provider.calls[0].scorer_names == ["factual", "tone"]
    assert provider.calls[0].scorer_weights == {"factual": 0.2, "tone": 0.8}

    # Weighted candidate overall = (0.9*0.2 + 0.8*0.8) = 0.82.
    assert job.eval_results is not None
    assert job.eval_results["candidate"]["overall"] == 0.82
    assert job.eval_results["gate"]["thresholds"]["min_aggregate_score"] == 0.8
    assert job.eval_results["gate"]["thresholds"]["max_regression_delta"] == 0.2
    assert job.status == "candidate_ready"


def test_run_eval_persists_eval_results_json(db_session: Session) -> None:
    """The eval_results JSON column has the canonical shape the UI reads."""
    _seed_job(db_session)
    run_eval(db_session, "RFN-E", FakeEvalProvider())

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-E")
    assert job is not None
    assert job.eval_results is not None
    payload = job.eval_results
    assert "candidate" in payload
    assert "baseline" in payload
    assert "deltas" in payload
    assert "eval_dataset_id" in payload
    assert "n_examples" in payload
    assert "gate" in payload


def test_run_eval_rejects_unknown_job(db_session: Session) -> None:
    with pytest.raises(LookupError):
        run_eval(db_session, "RFN-NONE", FakeEvalProvider())


def test_run_eval_rejects_missing_candidate(db_session: Session) -> None:
    _seed_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-E")
    assert job is not None
    job.candidate = None
    db_session.commit()
    with pytest.raises(LookupError, match=r"no candidate"):
        run_eval(db_session, "RFN-E", FakeEvalProvider())


def test_run_eval_rejects_queued_job(db_session: Session) -> None:
    _seed_job(db_session, status="queued", stage="triage")
    with pytest.raises(EvalStateError):
        run_eval(db_session, "RFN-E", FakeEvalProvider())


def test_run_eval_propagates_provider_error(db_session: Session) -> None:
    """Provider failures roll back and surface to the worker."""
    _seed_job(db_session)

    def boom(_request: object):  # type: ignore[no-untyped-def]
        raise EvalProviderError("eval dataset missing")

    provider = FakeEvalProvider(eval_callable=boom)
    with pytest.raises(EvalProviderError):
        run_eval(db_session, "RFN-E", provider)

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-E")
    assert job is not None
    assert job.status == "running"
    assert job.current_stage == "eval"
    assert job.eval_results is None


# ---------------------------------------------------------------------------
# CALIBER-namespaced tags (parity checklist §4)
# ---------------------------------------------------------------------------


def test_run_eval_emits_caliber_namespaced_tags(db_session: Session) -> None:
    """``eval_results`` carries the CALIBER aggregate fields the parity
    checklist requires. They live on the JSON column for now; a future
    MLflow-run integration logs them as tags on the parent run."""
    _seed_job(db_session)
    run_eval(db_session, "RFN-E", FakeEvalProvider())

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-E")
    assert job is not None
    tags = job.eval_results["caliber_tags"]  # type: ignore[index]
    assert "caliber.aggregate_score" in tags
    assert "caliber.test_case_count" in tags
    assert "caliber.max_regression_delta" in tags
    assert "caliber.regression_detected" in tags
    assert "caliber.gate_passed" in tags
    # The fake's default candidate (overall=0.94) and a passing gate.
    assert tags["caliber.aggregate_score"] == 0.94
    assert tags["caliber.gate_passed"] is True
    # No dimension regresses in the default fake.
    assert tags["caliber.regression_detected"] is False
    assert tags["caliber.max_regression_delta"] == 0.0


def test_run_eval_tags_flag_regression_when_dimension_drops(db_session: Session) -> None:
    """A candidate that regresses on any dimension fires
    ``caliber.regression_detected`` and reports the worst drop magnitude.

    ``run_eval`` passes ``baseline_content=None`` to the provider, so the
    fake's default cold-start path produces no baseline / no deltas. We
    inject the comparison directly via ``eval_callable`` to model the
    case the eval provider does have a baseline (the production path).
    """

    def with_regression(_request: EvalRequest) -> EvalComparison:
        candidate = ScoreSet(overall=0.90, dimensions={"factual": 0.95, "tone": 0.80})
        baseline = ScoreSet(overall=0.88, dimensions={"factual": 0.88, "tone": 0.89})
        deltas = {
            "overall": round(candidate.overall - baseline.overall, 4),
            "factual": round(0.95 - 0.88, 4),
            "tone": round(0.80 - 0.89, 4),
        }
        return EvalComparison(
            candidate=candidate,
            baseline=baseline,
            deltas=deltas,
            eval_dataset_id="default",
            n_examples=50,
        )

    _seed_job(db_session)
    provider = FakeEvalProvider(eval_callable=with_regression)
    run_eval(db_session, "RFN-E", provider)

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-E")
    assert job is not None
    tags = job.eval_results["caliber_tags"]  # type: ignore[index]
    assert tags["caliber.regression_detected"] is True
    # Tone dropped from 0.89 -> 0.80 = -0.09 regression.
    assert tags["caliber.max_regression_delta"] == pytest.approx(0.09, abs=1e-4)
    assert tags["caliber.gate_passed"] is False
    assert tags["caliber.test_case_count"] == 50
