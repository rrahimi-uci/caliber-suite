"""Regression tests for the deploy gate as *release evidence*.

The review's verdict was that the gate "uses a fake agent executor and measures
only successful completion — not expected output, judge quality, regression, cost,
or latency", "ignores two exposed threshold fields", and "is not mandatory for any
alias by default". Each of those is a separate assertion here:

* completion is not quality — a workflow that returns the wrong answer completes;
* a threshold the gate cannot evaluate fails **closed** rather than being ignored;
* latency, token spend, and regression-versus-deployed are measured; and
* a production promotion with no gate is refused by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.workflows import promoter
from caliber.workflows.deploy_gate import (
    SUPPORTED_THRESHOLDS,
    GateMetrics,
    evaluate_thresholds,
    percentile,
)
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.promoter import (
    DeployError,
    evaluate_deploy_gates,
    promote,
    requires_quality_gate,
)
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    fake_resolver,
    make_manifest,
    make_support_manifest,
    relax_release_quality_gate,
    seed_eval_dataset,
)

# ---------------------------------------------------------------------------
# Threshold vocabulary — unit level, no runtime needed
# ---------------------------------------------------------------------------


def test_every_supported_threshold_is_actually_wired() -> None:
    """The documented vocabulary and the evaluated vocabulary must be the same set.

    This is the property that keeps the fix from decaying: a threshold added to
    the docs/UI without an implementation would fail this test instead of
    silently doing nothing in production.
    """
    metrics = GateMetrics(
        n_examples=2,
        completed=2,
        scored_examples=2,
        pass_rate=1.0,
        overall=1.0,
        scorer_means={
            "exact_match": 1.0,
            "token_f1": 1.0,
            "contains_expected": 1.0,
            "non_empty": 1.0,
        },
        latencies_ms=[10.0, 20.0],
        tokens=[5, 7],
        baseline_overall=0.5,
    )
    outcomes = evaluate_thresholds(dict.fromkeys(SUPPORTED_THRESHOLDS, 0.0), metrics)
    assert {outcome.key for outcome in outcomes} == set(SUPPORTED_THRESHOLDS)
    # A 0.0 lower bound and a 0.0 upper bound cannot both hold for a positive
    # measurement, so assert only that every threshold produced a real verdict
    # rather than an "unsupported"/"unmeasurable" one.
    assert all(outcome.metric for outcome in outcomes), [
        outcome.detail for outcome in outcomes if not outcome.metric
    ]
    assert all(outcome.value is not None for outcome in outcomes)


def test_an_unsupported_threshold_fails_the_gate_closed() -> None:
    """Regression: two Inspector fields were silently ignored, so the gate read as
    configured while enforcing nothing. An unknown key must now *fail*."""
    metrics = GateMetrics(n_examples=1, completed=1, scored_examples=1, pass_rate=1.0, overall=1.0)
    outcomes = evaluate_thresholds({"max_tone_regression": 0.01}, metrics)
    assert len(outcomes) == 1
    assert outcomes[0].passed is False
    assert "not supported" in outcomes[0].detail
    assert "min_pass_rate" in outcomes[0].detail  # tells the operator what to use


def test_a_gate_with_no_thresholds_asserts_nothing_and_fails_closed() -> None:
    metrics = GateMetrics(n_examples=1, completed=1, scored_examples=1)
    outcomes = evaluate_thresholds({}, metrics)
    assert [o.passed for o in outcomes] == [False]
    assert "asserts nothing" in outcomes[0].detail


def test_a_non_numeric_threshold_fails_closed() -> None:
    metrics = GateMetrics(n_examples=1, completed=1, scored_examples=1)
    outcomes = evaluate_thresholds({"min_pass_rate": "high"}, metrics)  # type: ignore[dict-item]
    assert outcomes[0].passed is False
    assert "not a number" in outcomes[0].detail


def test_quality_thresholds_fail_closed_when_the_dataset_has_no_expected_output() -> None:
    """Grading against an absent expectation is meaningless. Reporting it as 0.0
    would read as "the workflow answered badly"; the gate says it cannot measure
    and names the fix."""
    metrics = GateMetrics(n_examples=3, completed=3, scored_examples=0, pass_rate=0.0)
    outcomes = evaluate_thresholds({"min_pass_rate": 0.0}, metrics)
    assert outcomes[0].passed is False
    assert "expected output" in outcomes[0].detail
    assert "min_completion_rate" in outcomes[0].detail
    # Completion is still measurable and still assertable.
    completion = evaluate_thresholds({"min_completion_rate": 1.0}, metrics)
    assert completion[0].passed is True


def test_overall_delta_fails_closed_without_a_baseline() -> None:
    metrics = GateMetrics(n_examples=1, completed=1, scored_examples=1, overall=0.9)
    outcomes = evaluate_thresholds({"min_overall_delta": 0.0}, metrics)
    assert outcomes[0].passed is False
    assert "currently deployed version" in outcomes[0].detail

    metrics.baseline_overall = 0.5
    improved = evaluate_thresholds({"min_overall_delta": 0.2}, metrics)
    assert improved[0].passed is True
    regressed = evaluate_thresholds({"min_overall_delta": 0.5}, metrics)
    assert regressed[0].passed is False


def test_latency_and_token_bounds_are_enforced() -> None:
    metrics = GateMetrics(
        n_examples=4,
        completed=4,
        scored_examples=4,
        latencies_ms=[10.0, 20.0, 30.0, 400.0],
        tokens=[10, 10, 10, 10],
    )
    outcomes = {
        o.key: o
        for o in evaluate_thresholds(
            {
                "max_avg_latency_ms": 100.0,
                "max_p95_latency_ms": 100.0,
                "max_avg_tokens": 10.0,
                "max_total_tokens": 39.0,
            },
            metrics,
        )
    }
    assert outcomes["max_avg_latency_ms"].value == pytest.approx(115.0)
    assert outcomes["max_avg_latency_ms"].passed is False  # mean 115 > 100
    assert outcomes["max_p95_latency_ms"].passed is False  # p95 = 400
    assert outcomes["max_avg_tokens"].passed is True
    assert outcomes["max_total_tokens"].passed is False  # 40 > 39


def test_percentile_is_nearest_rank() -> None:
    """A deploy gate replays tens of examples; an interpolated p95 would invent a
    latency no run actually produced."""
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0


# ---------------------------------------------------------------------------
# End-to-end grading through the promoter
# ---------------------------------------------------------------------------


def _graded_dataset(session: Session, *, answers: list[str]) -> None:
    dataset = CaliberEvalDataset(
        dataset_id="eval-graded",
        name="graded",
        owner="@test",
        status="active",
        version=1,
    )
    session.add(dataset)
    for index, answer in enumerate(answers):
        session.add(
            CaliberEvalDatasetExample(
                example_id=f"graded-{index}",
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input={"input": f"question {index}"},
                expected={"expected": answer},
                created_at=datetime(2026, 1, 1 + index, tzinfo=timezone.utc),
            )
        )
    session.flush()


def _graded_manifest(**thresholds: float) -> object:
    return parse_manifest(
        make_manifest(
            "wf",
            artifacts={"eval_datasets": {"graded": {"dataset_name": "graded"}}},
            deploy_gates={
                "quality": {
                    "type": "deploy_gate",
                    "dataset_ref": "graded",
                    "required_for_aliases": ["prod"],
                    "thresholds": dict(thresholds),
                    "scorers": ["exact_match"],
                    "pass_threshold": 1.0,
                }
            },
        )
    )


def _stub_execute(monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]) -> None:
    """Replace the runtime with a deterministic input→output mapping."""
    monkeypatch.setattr(
        promoter, "compile_workflow", lambda *args, **kwargs: SimpleNamespace(ir=None)
    )

    def _execute(plan, input_text, *, executor, preview=False, **kwargs):
        del plan, executor, preview, kwargs
        return SimpleNamespace(
            status="completed", output=outputs.get(input_text, ""), tokens=4, error=None
        )

    monkeypatch.setattr(promoter, "execute", _execute)


def test_a_completed_run_with_the_wrong_answer_fails_the_gate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core defect: completion is not quality. Both replays complete; one
    returns the wrong answer, so a quality gate must refuse the promotion."""
    _graded_dataset(db_session, answers=["yes", "no"])
    _stub_execute(monkeypatch, {"question 0": "yes", "question 1": "wrong"})

    result = evaluate_deploy_gates(
        db_session,
        _graded_manifest(min_pass_rate=1.0),
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )

    run = result.runs[0]
    assert result.passed is False
    assert run.metrics["completion_rate"] == 1.0  # everything completed...
    assert run.metrics["pass_rate"] == 0.5  # ...but half answered wrongly
    assert run.metrics["scored_examples"] == 2
    assert run.scorers == ["exact_match"]


def test_a_correct_run_passes_and_records_full_evidence(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _graded_dataset(db_session, answers=["yes", "no"])
    _stub_execute(monkeypatch, {"question 0": "yes", "question 1": "no"})

    result = evaluate_deploy_gates(
        db_session,
        _graded_manifest(min_pass_rate=1.0, min_exact_match=1.0, max_total_tokens=8.0),
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )

    run = result.runs[0]
    assert result.passed is True
    assert run.metrics["pass_rate"] == 1.0
    assert run.metrics["scorer.exact_match"] == 1.0
    assert run.metrics["total_tokens"] == 8.0
    assert "avg_latency_ms" in run.metrics
    # Evidence identity: which data, how much of it, and a checkable digest.
    assert run.dataset_id == "eval-graded"
    assert run.available_examples == 2
    assert run.sample_digest is not None and run.sample_digest.startswith("sha256:")
    assert {o["key"] for o in run.thresholds} == {
        "min_pass_rate",
        "min_exact_match",
        "max_total_tokens",
    }
    assert all(o["passed"] for o in run.thresholds)


def test_the_sample_digest_changes_when_the_graded_data_changes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest that did not move when the expected answers changed would be
    worthless as evidence."""
    _graded_dataset(db_session, answers=["yes", "no"])
    _stub_execute(monkeypatch, {"question 0": "yes", "question 1": "no"})
    manifest = _graded_manifest(min_pass_rate=1.0)
    first = evaluate_deploy_gates(
        db_session,
        manifest,
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    ).runs[0]

    example = db_session.get(CaliberEvalDatasetExample, "graded-1")
    assert example is not None
    example.expected = {"expected": "definitely not"}
    db_session.flush()

    second = evaluate_deploy_gates(
        db_session,
        manifest,
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    ).runs[0]
    assert first.sample_digest != second.sample_digest


def test_an_unsupported_threshold_blocks_a_real_promotion(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _graded_dataset(db_session, answers=["yes", "no"])
    _stub_execute(monkeypatch, {"question 0": "yes", "question 1": "no"})
    db_session.add(CaliberWorkflow(workflow_id="wf", name="Workflow", owner="@test"))
    version = CaliberWorkflowVersion(
        version_id="wfv-1",
        workflow_id="wf",
        version_number=1,
        status="published",
        manifest=_graded_manifest(max_tone_regression=0.01).to_dict(),
        manifest_hash="hash-1",
    )
    db_session.add(version)
    db_session.flush()

    with pytest.raises(promoter.DeployGateFailedError) as excinfo:
        promote(
            db_session,
            "wf",
            "prod",
            version,
            actor="@ops",
            # The threshold vocabulary is the subject; grading is stubbed, so the
            # production graded-executor requirement is opted out of here.
            config=CaliberConfig(release_require_graded_executor_for_environment_classes=""),
        )
    assert "not supported" in str(excinfo.value.detail)


def test_min_overall_delta_compares_against_the_deployed_version(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``min_overall_delta`` was an exposed-but-ignored field. It now replays the
    alias's *currently deployed* version on the same sample, which is what makes
    it a regression check rather than decoration."""
    _graded_dataset(db_session, answers=["yes", "no"])
    db_session.add(CaliberWorkflow(workflow_id="wf", name="Workflow", owner="@test"))
    baseline_manifest = make_manifest("wf")
    baseline = CaliberWorkflowVersion(
        version_id="wfv-baseline",
        workflow_id="wf",
        version_number=1,
        status="published",
        manifest=baseline_manifest,
        manifest_hash="hash-baseline",
    )
    db_session.add(baseline)
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="wfd-1",
            workflow_id="wf",
            alias="prod",
            version_id="wfv-baseline",
            status="active",
            deployed_by="@test",
            deployed_at=datetime.now(timezone.utc),
            rollback_checkpoint=[],
        )
    )
    db_session.flush()

    # The candidate answers both correctly; the baseline gets one wrong. Both are
    # replayed through the same stub, distinguished by which IR is compiled.
    # The compiled IR carries the compile tag so the stubbed runtime can tell the
    # candidate replay from the baseline replay.
    monkeypatch.setattr(
        promoter,
        "compile_workflow",
        lambda manifest, **kwargs: SimpleNamespace(
            ir=SimpleNamespace(nodes={}, version=kwargs.get("version"))
        ),
    )
    calls: list[str] = []

    def _execute(plan, input_text, *, executor, preview=False, **kwargs):
        del executor, preview, kwargs
        is_baseline = getattr(plan.ir, "version", None) == "gate-baseline"
        calls.append("baseline" if is_baseline else "candidate")
        answers = {"question 0": "yes", "question 1": "no"}
        if is_baseline and input_text == "question 1":
            return SimpleNamespace(status="completed", output="wrong", tokens=1, error=None)
        return SimpleNamespace(
            status="completed", output=answers.get(input_text, ""), tokens=1, error=None
        )

    monkeypatch.setattr(promoter, "execute", _execute)

    result = evaluate_deploy_gates(
        db_session,
        _graded_manifest(min_overall_delta=0.4),
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )
    run = result.runs[0]
    assert "baseline" in calls and "candidate" in calls
    assert run.metrics["baseline_overall"] == pytest.approx(0.5)
    assert run.metrics["overall_delta"] == pytest.approx(0.5)
    assert result.passed is True


# ---------------------------------------------------------------------------
# The production requirement itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["prod", "production", "PROD", "prod-eu"])
def test_a_quality_gate_is_required_for_every_production_spelling(alias: str) -> None:
    assert requires_quality_gate(alias, CaliberConfig()) is True


@pytest.mark.parametrize("alias", ["dev", "staging", "sandbox"])
def test_lower_environments_deploy_without_a_gate(alias: str) -> None:
    assert requires_quality_gate(alias, CaliberConfig()) is False


def test_promoting_to_production_without_a_gate_is_refused(db_session: Session) -> None:
    db_session.add(CaliberWorkflow(workflow_id="wf", name="Workflow", owner="@test"))
    version = CaliberWorkflowVersion(
        version_id="wfv-1",
        workflow_id="wf",
        version_number=1,
        status="published",
        manifest=make_manifest("wf"),
        manifest_hash="hash-1",
    )
    db_session.add(version)
    db_session.flush()

    with pytest.raises(DeployError, match="requires a deploy gate"):
        promote(db_session, "wf", "prod", version, actor="@ops", config=CaliberConfig())

    # Nothing was rotated.
    assert db_session.query(CaliberWorkflowDeployment).count() == 0


def test_the_requirement_can_be_relaxed_for_a_development_install(db_session: Session) -> None:
    db_session.add(CaliberWorkflow(workflow_id="wf", name="Workflow", owner="@test"))
    version = CaliberWorkflowVersion(
        version_id="wfv-1",
        workflow_id="wf",
        version_number=1,
        status="published",
        manifest=make_manifest("wf"),
        manifest_hash="hash-1",
    )
    db_session.add(version)
    db_session.flush()

    config = CaliberConfig(release_require_quality_gate_for_environment_classes="")
    result = promote(db_session, "wf", "prod", version, actor="@ops", config=config)
    assert result.rotated is True
    assert result.deployment is not None
    assert result.deployment.environment == "production"


def test_human_approval_is_one_configuration_switch(
    client: TestClient, db_session: Session
) -> None:
    """The approval machinery is wired, not dormant: setting the environment-class
    switch makes a production promotion pause for sign-off."""
    seed_eval_dataset(db_session)
    manifest = make_support_manifest(
        "approval_wf",
        deploy_gates={
            "support_eval": {
                "type": "deploy_gate",
                "dataset_ref": "support_eval",
                "required_for_aliases": ["prod"],
                "thresholds": {"min_completion_rate": 1.0},
            }
        },
    )
    wid, vid = create_and_publish(client, workflow_name="Approval", manifest=manifest)
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "release_require_human_approval_for_environment_classes": "production",
            # The subject here is the approval switch. The suite grades with the
            # deterministic fake, which production otherwise refuses as release
            # evidence — see tests/test_deploy_gate_executor.py for that default.
            "release_require_graded_executor_for_environment_classes": "",
        }
    )

    response = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid}
    )
    assert response.status_code == 202, response.text
    assert response.json()["data"]["rotated"] is False
    assert response.json()["data"]["promotion"]["status"] == "pending"
    assert client.get(f"{PREFIX}/workflows/{wid}/deployments").json()["data"] == []


def test_the_deployment_environment_column_is_populated(
    client: TestClient, db_session: Session
) -> None:
    """``environment`` was dormant — promote could not set it and rotation never
    wrote it, so nothing could report what a deployment served."""
    wid, vid = create_and_publish(client, workflow_name="EnvCol")
    relax_release_quality_gate(client)
    assert (
        client.post(
            f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{PREFIX}/workflows/{wid}/deployments/staging/promote", json={"version_id": vid}
        ).status_code
        == 200
    )

    rows = {
        row.alias: row.environment
        for row in db_session.query(CaliberWorkflowDeployment)
        .filter(CaliberWorkflowDeployment.workflow_id == wid)
        .all()
    }
    assert rows == {"prod": "production", "staging": "staging"}
