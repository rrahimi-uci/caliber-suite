"""B10.4 release-evaluation recovery and fenced completion scenarios."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from caliber.db.models import (
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceReleaseEvaluationAttempt,
)
from caliber.orchestrator.workspace_release_evaluator import WorkspaceReleaseEvaluator
from caliber.workspace_release_service import (
    EVALUATION_ATTEMPT_FAILED,
    EVALUATION_ATTEMPT_ORPHANED,
    EVALUATION_FAILED,
    WorkspaceReleaseLeaseError,
    claim_workspace_release_evaluation,
    complete_workspace_release_evaluation,
    record_workspace_release_evaluation_attempt_runs,
)
from tests.egi_scenarios.conftest import EDITOR_A, PROJECT_A, ReferenceApp, Workspaces, seed_release
from tests.egi_scenarios.test_b02_admission import _call
from tests.test_workspace_release_evaluation_execution import _factory, _seed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_public_evaluation_api_queues_and_lists_an_attempt(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
) -> None:
    """The public release-evaluation API records one queued, project-scoped attempt."""
    coordinates = seed_release(reference_app, PROJECT_A, name="v13-public")
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"feature_release_candidates_enabled": True}
    )
    with reference_app.session_factory() as session:
        release = session.get(CaliberWorkspaceRelease, coordinates["workspace_release_id"])
        assert release is not None
        release.status = "draft"
        session.commit()

    prefix = f"/projects/{PROJECT_A}/releases/{coordinates['workspace_release_id']}"
    evaluated = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"{prefix}/evaluate",
        {
            "idempotency_key": "v13-public-evaluation",
            "evaluation_plan_sha256": "a" * 64,
            "input_sha256": "b" * 64,
        },
    )
    assert evaluated.status_code == 202, evaluated.text
    evaluation_id = evaluated.json()["data"]["evaluation_id"]
    assert evaluated.json()["data"]["status"] == "queued"

    listed = _call(
        reference_app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"{prefix}/evaluations",
    )
    assert listed.status_code == 200, listed.text
    assert [row["evaluation_id"] for row in listed.json()["data"]] == [evaluation_id]


def test_worker_records_provider_run_before_completion_crash(tmp_path: Any) -> None:
    """A crash after the provider returns leaves the run linked to the old claim."""

    factory = _factory(tmp_path)
    registry, provider = _seed(factory)
    provider.evaluation_run_ids = ("mlflow-run-before-crash",)
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=1, execution_lease_seconds=30),
        eval_provider=provider,
        adapter_registry=registry,
    )
    worker._complete = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("simulated process crash after provider result")
    )

    with pytest.raises(RuntimeError, match="simulated process crash"):
        worker.tick()

    with factory() as session:
        evaluation = session.execute(select(CaliberWorkspaceReleaseEvaluation)).scalar_one()
        assert evaluation.status == "running"
        assert evaluation.linked_evaluation_run_ids == ["mlflow-run-before-crash"]
        attempt = session.execute(
            select(CaliberWorkspaceReleaseEvaluationAttempt).where(
                CaliberWorkspaceReleaseEvaluationAttempt.evaluation_id == evaluation.evaluation_id
            )
        ).scalar_one()
        assert attempt.provider_run_ids == ["mlflow-run-before-crash"]
        assert attempt.authoritative is False

        recovered = claim_workspace_release_evaluation(
            session,
            evaluation,
            worker_id="restart-worker",
            lease_seconds=30,
            now=_now() + timedelta(seconds=2),
        )
        assert recovered.attempt_number == 2


def test_evaluator_crash_recovers(tmp_path: Any) -> None:
    """An external run remains auditable while only the recovered claim settles."""

    factory = _factory(tmp_path)
    _registry, _provider = _seed(factory)
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=1, execution_lease_seconds=30),
    )
    first = worker._claim_oldest(None)
    assert first is not None and first.attempt_number == 1

    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, first.evaluation_id)
        assert evaluation is not None
        record_workspace_release_evaluation_attempt_runs(
            session,
            evaluation,
            attempt_number=first.attempt_number,
            worker_id=first.worker_id,
            evaluation_run_ids=("mlflow-run-orphan", "mlflow-run-orphan"),
        )
        session.commit()

    recovered_at = _now() + timedelta(seconds=2)
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, first.evaluation_id)
        assert evaluation is not None
        recovered = claim_workspace_release_evaluation(
            session,
            evaluation,
            worker_id="recovery-worker",
            lease_seconds=30,
            now=recovered_at,
        )
        assert recovered.attempt_number == 2
        assert recovered.linked_evaluation_run_ids == ["mlflow-run-orphan"]
        attempts = (
            session.execute(
                select(CaliberWorkspaceReleaseEvaluationAttempt).order_by(
                    CaliberWorkspaceReleaseEvaluationAttempt.attempt_number
                )
            )
            .scalars()
            .all()
        )
        assert attempts[0].status == EVALUATION_ATTEMPT_ORPHANED
        assert attempts[0].provider_run_ids == ["mlflow-run-orphan"]
        assert attempts[0].authoritative is False

        with pytest.raises(WorkspaceReleaseLeaseError, match="attempt is no longer current"):
            complete_workspace_release_evaluation(
                session,
                recovered,
                worker_id="recovery-worker",
                attempt_number=first.attempt_number,
                result="blocked",
                expected_release_lock_version=2,
                error_code="stale_completion",
                error_summary="stale worker result must not settle the release",
                now=recovered_at + timedelta(seconds=1),
            )

        completed = complete_workspace_release_evaluation(
            session,
            recovered,
            worker_id="recovery-worker",
            attempt_number=recovered.attempt_number,
            result="blocked",
            expected_release_lock_version=2,
            error_code="provider_timeout",
            error_summary="the recovered provider attempt did not complete",
            evaluation_run_ids=["mlflow-run-orphan", "mlflow-run-recovery"],
            now=recovered_at + timedelta(seconds=1),
        )
        assert completed.status == EVALUATION_FAILED
        attempts = (
            session.execute(
                select(CaliberWorkspaceReleaseEvaluationAttempt).order_by(
                    CaliberWorkspaceReleaseEvaluationAttempt.attempt_number
                )
            )
            .scalars()
            .all()
        )
        assert attempts[1].status == EVALUATION_ATTEMPT_FAILED
        assert attempts[1].authoritative is True
        assert attempts[1].provider_run_ids == ["mlflow-run-orphan", "mlflow-run-recovery"]


def test_recovery_before_provider_result_keeps_attempt_history(tmp_path: Any) -> None:
    """A crash before the provider returns still burns the old claim number."""

    factory = _factory(tmp_path)
    _registry, _provider = _seed(factory)
    worker = WorkspaceReleaseEvaluator(
        factory,
        SimpleNamespace(workspace_release_evaluator_lease_seconds=1, execution_lease_seconds=30),
    )
    first = worker._claim_oldest(None)
    assert first is not None
    with factory() as session:
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, first.evaluation_id)
        assert evaluation is not None
        recovered = claim_workspace_release_evaluation(
            session,
            evaluation,
            worker_id="restart-worker",
            lease_seconds=30,
            now=_now() + timedelta(seconds=2),
        )
        attempts = (
            session.execute(
                select(CaliberWorkspaceReleaseEvaluationAttempt).order_by(
                    CaliberWorkspaceReleaseEvaluationAttempt.attempt_number
                )
            )
            .scalars()
            .all()
        )
        assert recovered.attempt_number == 2
        assert attempts[0].status == EVALUATION_ATTEMPT_ORPHANED
        assert attempts[0].provider_run_ids == []
