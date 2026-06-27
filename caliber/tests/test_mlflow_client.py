"""Tests for the MLflow assessment client abstraction.

The Protocol + Fake pair is what the poller depends on. The production impl
(``MLflowAssessmentClientImpl``) needs an MLflow server and is exercised by
a separate integration test that runs only when ``MLFLOW_TRACKING_URI`` is
set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from caliber.mlflow_client import (
    AssessmentInfo,
    FakeMLflowAssessmentClient,
    _severity_from_feedback,
)


def _info(
    *,
    assessment_id: str,
    experiment_id: str,
    created_at: datetime,
    severity: str = "standard",
) -> AssessmentInfo:
    return AssessmentInfo(
        assessment_id=assessment_id,
        trace_id=f"tr-{assessment_id}",
        experiment_id=experiment_id,
        created_at=created_at,
        category="feedback",
        free_text="...",
        severity=severity,
    )


def test_fake_client_filters_by_experiment() -> None:
    now = datetime.now(timezone.utc)
    client = FakeMLflowAssessmentClient(
        [
            _info(assessment_id="a", experiment_id="exp-1", created_at=now),
            _info(assessment_id="b", experiment_id="exp-2", created_at=now),
        ]
    )
    results = list(
        client.list_assessments_since(experiment_ids=["exp-1"], since=now - timedelta(hours=1))
    )
    assert [r.assessment_id for r in results] == ["a"]


def test_fake_client_filters_by_since() -> None:
    now = datetime.now(timezone.utc)
    client = FakeMLflowAssessmentClient(
        [
            _info(assessment_id="old", experiment_id="exp-1", created_at=now - timedelta(hours=2)),
            _info(assessment_id="new", experiment_id="exp-1", created_at=now),
        ]
    )
    results = list(
        client.list_assessments_since(experiment_ids=["exp-1"], since=now - timedelta(hours=1))
    )
    assert [r.assessment_id for r in results] == ["new"]


def test_fake_client_sorts_newest_first() -> None:
    now = datetime.now(timezone.utc)
    client = FakeMLflowAssessmentClient(
        [
            _info(
                assessment_id="old", experiment_id="exp-1", created_at=now - timedelta(minutes=5)
            ),
            _info(assessment_id="new", experiment_id="exp-1", created_at=now),
            _info(
                assessment_id="mid", experiment_id="exp-1", created_at=now - timedelta(minutes=2)
            ),
        ]
    )
    results = list(
        client.list_assessments_since(experiment_ids=["exp-1"], since=now - timedelta(hours=1))
    )
    assert [r.assessment_id for r in results] == ["new", "mid", "old"]


def test_fake_client_handles_empty_experiment_list() -> None:
    client = FakeMLflowAssessmentClient([])
    results = list(
        client.list_assessments_since(
            experiment_ids=[], since=datetime.now(timezone.utc) - timedelta(hours=1)
        )
    )
    assert results == []


def test_fake_client_caps_at_max_results() -> None:
    now = datetime.now(timezone.utc)
    client = FakeMLflowAssessmentClient(
        [
            _info(
                assessment_id=f"a-{i}",
                experiment_id="exp-1",
                created_at=now - timedelta(seconds=i),
            )
            for i in range(10)
        ]
    )
    results = list(
        client.list_assessments_since(
            experiment_ids=["exp-1"],
            since=now - timedelta(hours=1),
            max_results=3,
        )
    )
    assert len(results) == 3


def test_severity_from_feedback_critical_for_negative() -> None:
    assert _severity_from_feedback(False) == "critical"
    assert _severity_from_feedback(0) == "critical"
    assert _severity_from_feedback("down") == "critical"
    assert _severity_from_feedback("BAD") == "critical"


def test_severity_from_feedback_standard_for_positive_or_missing() -> None:
    assert _severity_from_feedback(True) == "standard"
    assert _severity_from_feedback(1) == "standard"
    assert _severity_from_feedback(None) == "standard"
    assert _severity_from_feedback("ok") == "standard"
