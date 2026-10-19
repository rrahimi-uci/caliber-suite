"""Workflow evaluation results say what produced them (alpha gap P1-22, after P0-4).

Calibrating a prompt stamps ``provider`` and ``simulated`` on its evidence so a canned score
is never mistaken for a measurement. A workflow calibration run under the default fake LLM
replays the candidate and the baseline through the deterministic executor and scores them
structurally, and its results carried no such marker.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

import caliber.orchestrator.workflow_stages as stages
from caliber.config import CaliberConfig
from caliber.workflows.calibration import WorkflowCalibrationError, WorkflowCalibrationResult
from caliber.workflows.refinement import WorkflowEvalResult
from tests.test_workflow_stages import _seed_common, _seed_job, _seed_version
from tests.workflow_helpers import make_manifest


def _config(**environ: str) -> CaliberConfig:
    return CaliberConfig.load(environ={"CALIBER_DATABASE_URL": "sqlite+pysqlite://", **environ})


def test_the_default_fake_llm_is_simulated_and_scored_structurally() -> None:
    assert stages.workflow_eval_provenance(None) == {
        "provider": "fake",
        "simulated": True,
        "scorer": "structural",
    }
    assert stages.workflow_eval_provenance(_config())["simulated"] is True


@pytest.mark.parametrize("provider", ["fake", "deterministic", "FAKE"])
def test_every_spelling_of_the_fake_provider_is_simulated(provider: str) -> None:
    config = _config(CALIBER_LLM_PROVIDER=provider)
    assert stages.workflow_eval_provenance(config)["simulated"] is True


def test_a_real_provider_is_not_simulated_but_is_still_structural_without_the_judge() -> None:
    config = _config(CALIBER_LLM_PROVIDER="openai")
    provenance = stages.workflow_eval_provenance(config)
    assert provenance["provider"] == "openai"
    assert provenance["simulated"] is False
    assert provenance["scorer"] == "structural"


def test_the_llm_judge_is_named_only_when_it_is_actually_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stages, "llm_judge_status", lambda config: {"available": True, "provider": "openai"}
    )
    provenance = stages.workflow_eval_provenance(_config(CALIBER_LLM_PROVIDER="openai"))
    assert provenance["scorer"] == "llm_judge"
    assert provenance["simulated"] is False


def _result(passed: bool) -> WorkflowEvalResult:
    return WorkflowEvalResult(
        passed=passed,
        baseline_scores={"quality": 0.7},
        candidate_scores={"quality": 0.9 if passed else 0.6},
        deltas={"quality": 0.2 if passed else -0.1},
        n_examples=1,
        gate={"passed": passed, "reasons": [] if passed else ["quality dropped"]},
    )


@pytest.mark.parametrize("passed", [True, False])
def test_a_workflow_eval_stamps_the_provenance_whether_it_passes_or_not(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, passed: bool
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
    monkeypatch.setattr(stages, "evaluate_candidate", lambda *a, **k: _result(passed))

    updated = stages.run_workflow_eval(db_session, job, actor="@tester")

    assert updated.status == ("candidate_ready" if passed else "rejected")
    results: dict[str, Any] = updated.eval_results or {}
    assert results["provider"] == "fake"
    assert results["simulated"] is True
    assert results["scorer"] == "structural"
    assert results["workflow"] is True


def test_a_calibration_eval_stamps_the_provenance_too(db_session: Session) -> None:
    _seed_common(db_session)
    base = make_manifest("wf")
    winner = {
        "candidate_id": "cal-1",
        "accepted": True,
        "scores": {"quality": 0.9},
        "deltas": {"quality": 0.2},
        "gate": {"passed": True},
    }
    job = _seed_job(
        db_session,
        stage="eval",
        diagnosis={"root_cause": "x"},
        candidate={
            "candidate_manifest": base,
            "baseline_manifest": base,
            "calibration": True,
            "calibration_candidates": [winner],
            "calibration_winner_id": "cal-1",
            "calibration_baseline_scores": {"quality": 0.7},
            "calibration_n_examples": 4,
            "calibration_gate": {"passed": True},
        },
    )
    db_session.commit()

    updated = stages.run_workflow_eval(db_session, job, actor="@tester")

    assert updated.status == "candidate_ready"
    results: dict[str, Any] = updated.eval_results or {}
    assert results["calibration"] is True
    assert results["n_examples"] == 4
    assert results["simulated"] is True
    assert results["scorer"] == "structural"
    assert results["provider"] == "fake"


def test_a_real_provider_config_reaches_the_stamp_through_the_stage(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr(stages, "evaluate_candidate", lambda *a, **k: _result(True))
    monkeypatch.setattr(stages, "_workflow_manifest_executor_factory", lambda config: None)
    monkeypatch.setattr(stages, "build_llm_judge_scorer", lambda config: None)

    updated = stages.run_workflow_eval(
        db_session, job, actor="@tester", config=_config(CALIBER_LLM_PROVIDER="openai")
    )

    results: dict[str, Any] = updated.eval_results or {}
    assert results["provider"] == "openai"
    assert results["simulated"] is False


def _calibration_job(db_session: Session) -> Any:
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
    return job


def _stub_calibration(monkeypatch: pytest.MonkeyPatch, evaluate: Any) -> None:
    monkeypatch.setattr(stages, "resolve_workflow_calibration_examples", lambda *a, **k: [])
    monkeypatch.setattr(stages, "generate_workflow_calibration_candidates", lambda *a, **k: [])
    monkeypatch.setattr(stages, "evaluate_workflow_calibration_candidates", evaluate)
    monkeypatch.setattr(stages, "_workflow_manifest_executor_factory", lambda config: None)
    monkeypatch.setattr(stages, "build_llm_judge_scorer", lambda config: None)


@pytest.mark.parametrize(
    ("environ", "provider", "simulated"),
    [({}, "fake", True), ({"CALIBER_LLM_PROVIDER": "openai"}, "openai", False)],
)
def test_a_calibration_with_no_candidate_still_says_what_measured_its_baseline(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    provider: str,
    simulated: bool,
) -> None:
    """The run ends ``rejected`` with no winner, but its baseline was measured: say by what."""
    job = _calibration_job(db_session)
    _stub_calibration(
        monkeypatch,
        lambda *a, **k: WorkflowCalibrationResult(
            passed=False,
            baseline_scores={"quality": 0.9},
            candidates=[],
            winner=None,
            n_examples=20,
            low_confidence=False,
            objective="quality",
            gate={"passed": False, "reasons": ["no candidate met calibration gates"]},
        ),
    )

    updated = stages.run_workflow_candidate(
        db_session, job, actor="@tester", config=_config(**environ)
    )

    assert updated.status == "rejected"
    results: dict[str, Any] = updated.eval_results or {}
    assert results["baseline"]["overall"] == 0.9
    assert results["provider"] == provider
    assert results["simulated"] is simulated
    assert results["scorer"] == "structural"


def test_a_calibration_that_errors_still_says_what_ran_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _calibration_job(db_session)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise WorkflowCalibrationError("the executor could not be built")

    _stub_calibration(monkeypatch, _boom)

    updated = stages.run_workflow_candidate(db_session, job, actor="@tester")

    assert updated.status == "rejected"
    results: dict[str, Any] = updated.eval_results or {}
    assert results["gate"]["reasons"] == ["the executor could not be built"]
    assert results["simulated"] is True
    assert results["provider"] == "fake"
