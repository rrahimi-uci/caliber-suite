"""Tests for the Prometheus metrics module + endpoint."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberRegressionRun,
    CaliberVerificationItem,
)
from caliber.observability import metrics
from caliber.regression import candidate_hash
from caliber.routes.metrics import METRICS_PATH


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    """Each test gets a clean metric registry — counters don't leak across tests."""
    metrics.reset_metrics_for_test()


def test_registry_exposes_expected_metric_names() -> None:
    names = set(metrics.list_metric_names())
    assert "caliber_verification_items" in names
    assert "caliber_jobs" in names
    assert "caliber_approvals" in names
    assert "caliber_promotions" in names
    assert "caliber_rollbacks" in names
    assert "caliber_stage_duration_seconds" in names
    assert "caliber_verification_queue_depth" in names
    assert "caliber_approvals_pending" in names
    assert "caliber_jobs_in_flight" in names


def test_render_returns_prometheus_text_format() -> None:
    metrics.record_promotion(agent_id="support-agent", artifact_type="prompt")
    body = metrics.render().decode("utf-8")
    # Standard exposition format header + counter line + the _created series.
    assert "# HELP caliber_promotions_total" in body
    assert "# TYPE caliber_promotions_total counter" in body
    assert 'caliber_promotions_total{agent_id="support-agent",artifact_type="prompt"} 1.0' in body


def test_helper_facades_increment_their_metrics() -> None:
    metrics.record_verification_outcome("a", "verified")
    metrics.record_job_terminal("a", "prompt", "completed")
    metrics.record_approval_decision("a", "approved")
    metrics.record_rollback("a")
    metrics.observe_stage_duration("a", "eval", 1.5)
    body = metrics.render().decode("utf-8")
    assert 'caliber_verification_items_total{agent_id="a",outcome="verified"} 1.0' in body
    assert 'caliber_jobs_total{agent_id="a",artifact_type="prompt",status="completed"} 1.0' in body
    assert 'caliber_approvals_total{agent_id="a",decision="approved"} 1.0' in body
    assert 'caliber_rollbacks_total{agent_id="a"} 1.0' in body
    assert 'caliber_stage_duration_seconds_count{agent_id="a",stage="eval"} 1.0' in body


def test_gauges_can_be_set_and_overwritten() -> None:
    metrics.set_queue_depth("critical", 5)
    metrics.set_approvals_pending(2)
    metrics.set_jobs_in_flight("running", 3)
    body = metrics.render().decode("utf-8")
    assert 'caliber_verification_queue_depth{severity="critical"} 5.0' in body
    assert "caliber_approvals_pending 2.0" in body
    assert 'caliber_jobs_in_flight{status="running"} 3.0' in body

    metrics.set_approvals_pending(7)
    assert "caliber_approvals_pending 7.0" in metrics.render().decode("utf-8")


def test_metrics_endpoint_returns_text_format(client: TestClient) -> None:
    metrics.record_promotion(agent_id="agent-x", artifact_type="prompt")
    response = client.get(METRICS_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "caliber_promotions_total" in response.text


def test_apply_endpoint_increments_metrics(client: TestClient, db_session: Session) -> None:
    """End-to-end: applying a candidate_ready job should bump promotion counters."""
    db_session.add(
        CaliberAgentConfig(
            agent_id="agent",
            experiment_id="exp",
            name="A",
            owner="@x",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.flush()
    db_session.add(
        CaliberVerificationItem(
            item_id="FB-M",
            agent_id="agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    db_session.flush()
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-M",
            agent_id="agent",
            primary_item_id="FB-M",
            artifact_type="prompt",
            status="candidate_ready",
            current_stage="done",
            bundle_targets=[],
            eval_results={},
            candidate={"content": "new", "artifact_type": "prompt"},
        )
    )
    db_session.add(
        CaliberRegressionRun(
            run_id="RR-AP-M",
            job_id="RFN-M",
            approval_id=None,
            agent_id="agent",
            candidate_hash=candidate_hash("new"),
            status="passed",
            required_for_approval=True,
            dataset_ids=["default"],
            trace_sample_ids=[],
            baseline_scores={"overall": 0.88},
            candidate_scores={"overall": 0.94},
            deltas={"overall": 0.06},
            regressions=[],
            gate={"passed": True, "reasons": []},
        )
    )
    db_session.commit()

    response = client.post("/ajax-api/2.0/mlflow/caliber/jobs/RFN-M/apply", json={})
    assert response.status_code == 200

    body = client.get(METRICS_PATH).text
    assert 'caliber_promotions_total{agent_id="agent",artifact_type="prompt"} 1.0' in body
    assert (
        'caliber_jobs_total{agent_id="agent",artifact_type="prompt",status="applied"} 1.0' in body
    )


def test_reset_clears_unlabeled_histogram() -> None:
    """Regression (#21): reset_metrics_for_test must zero an UNLABELED histogram
    (workflow_compile_seconds) — it has no _value and isn't a Gauge, so the old
    reset fell through and leaked observations across tests."""
    from caliber.observability import metrics

    metrics.record_workflow_compile(ok=True, duration_ms=12.0)
    metrics.reset_metrics_for_test()
    body = metrics.render().decode()
    assert "caliber_workflow_compile_seconds_count 0.0" in body
