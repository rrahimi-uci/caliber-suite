"""Integration tests for ``/caliber/jobs``."""

from __future__ import annotations

import types

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.routes import jobs as jobs_routes
from caliber.routes.jobs import DETAIL_PATH, LIST_PATH, REQUEST_CHANGES_PATH


def _seed(session: Session) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-1",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add_all(
        [
            CaliberRefinementJob(
                job_id="RFN-A",
                agent_id="support-agent",
                workflow_id="WF-1",
                primary_item_id="FB-1",
                artifact_type="prompt",
                status="queued",
                current_stage="triage",
                bundle_targets=[],
            ),
            CaliberRefinementJob(
                job_id="RFN-B",
                agent_id="support-agent",
                workflow_id="WF-1",
                primary_item_id="FB-1",
                artifact_type="prompt",
                status="running",
                current_stage="evidence",
                bundle_targets=[],
            ),
            CaliberRefinementJob(
                job_id="RFN-C",
                agent_id="support-agent",
                workflow_id="WF-2",
                primary_item_id="FB-1",
                artifact_type="skill",
                skill_name="tool-use",
                status="completed",
                current_stage="done",
                bundle_targets=[],
            ),
        ]
    )
    session.commit()


def test_list_jobs_returns_all_unfiltered(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH)
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-A", "RFN-B", "RFN-C"}


def test_list_jobs_filter_by_status(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH, params={"status": "running"})
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-B"}


def test_list_jobs_filter_by_stage(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH, params={"stage": "triage"})
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-A"}


def test_list_jobs_filter_by_workflow_id(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(LIST_PATH, params={"workflow_id": "WF-1"})
    assert response.status_code == 200
    ids = {item["job_id"] for item in response.json()["data"]}
    assert ids == {"RFN-A", "RFN-B"}


def test_list_jobs_invalid_status_returns_400(client: TestClient) -> None:
    response = client.get(LIST_PATH, params={"status": "bogus"})
    assert response.status_code == 400


def test_list_jobs_invalid_stage_returns_400(client: TestClient) -> None:
    response = client.get(LIST_PATH, params={"stage": "bogus"})
    assert response.status_code == 400


def test_get_job_returns_record(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-A"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["job_id"] == "RFN-A"
    assert data["current_stage"] == "triage"
    assert data["status"] == "queued"


def test_get_job_includes_skill_name(client: TestClient, db_session: Session) -> None:
    _seed(db_session)
    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-C"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artifact_type"] == "skill"
    assert data["skill_name"] == "tool-use"


def test_get_job_404_when_missing(client: TestClient) -> None:
    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-NONE"))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /jobs/{id}/targets — bundle review
# ---------------------------------------------------------------------------


def _seed_bundle_job(
    session: Session,
    job_id: str,
    bundle_targets: list[dict[str, object]],
) -> None:
    if session.get(CaliberAgentConfig, "support-agent") is None:
        session.add(
            CaliberAgentConfig(
                agent_id="support-agent",
                experiment_id="exp",
                name="Support",
                owner="@sarah",
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
            )
        )
        session.flush()
        session.add(
            CaliberVerificationItem(
                item_id="FB-1",
                agent_id="support-agent",
                category="hallucination",
                free_text="...",
                severity="critical",
                status="verified",
            )
        )
        session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-1",
            artifact_type="prompt",
            status="running",
            current_stage="candidate",
            bundle_targets=bundle_targets,
        )
    )
    session.commit()


def test_get_job_targets_single_agent_returns_one_row(
    client: TestClient, db_session: Session
) -> None:
    """A job with no bundle_targets entries returns one row — its own
    agent/artifact_type — so the UI doesn't need a special case."""
    _seed_bundle_job(db_session, "RFN-SOLO", [])
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-SOLO/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bundle_size"] == 1
    assert len(data["targets"]) == 1
    assert data["targets"][0]["agent_id"] == "support-agent"
    assert data["targets"][0]["artifact_type"] == "prompt"


def test_get_job_targets_bundle_returns_one_row_per_agent(
    client: TestClient, db_session: Session
) -> None:
    bundle = [
        {"agent_id": "coordinator", "artifact_type": "coordinator_policy", "role": "lead"},
        {"agent_id": "router", "artifact_type": "routing_policy"},
        {"agent_id": "support-agent", "artifact_type": "role_prompt"},
    ]
    _seed_bundle_job(db_session, "RFN-BUNDLE", bundle)

    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-BUNDLE/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bundle_size"] == 3
    by_agent = {row["agent_id"]: row for row in data["targets"]}
    assert by_agent["coordinator"]["artifact_type"] == "coordinator_policy"
    assert by_agent["coordinator"]["role"] == "lead"
    assert by_agent["router"]["artifact_type"] == "routing_policy"
    assert by_agent["support-agent"]["artifact_type"] == "role_prompt"


def test_get_job_targets_preserves_extra_keys(client: TestClient, db_session: Session) -> None:
    """``bundle_targets`` entries can carry richer metadata; we shouldn't
    drop it (the UI may render blast-radius scores, etc.)."""
    bundle = [
        {
            "agent_id": "coordinator",
            "artifact_type": "coordinator_policy",
            "blast_radius": 0.78,
            "current_version": 5,
        }
    ]
    _seed_bundle_job(db_session, "RFN-RICH", bundle)
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-RICH/targets")
    assert response.status_code == 200
    target = response.json()["data"]["targets"][0]
    assert target["blast_radius"] == 0.78
    assert target["current_version"] == 5


def test_get_job_targets_404_when_missing(client: TestClient) -> None:
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-GHOST/targets")
    assert response.status_code == 404


def test_get_job_targets_ignores_non_dict_entries(client: TestClient, db_session: Session) -> None:
    """Defensive: a malformed bundle entry shouldn't crash the endpoint."""
    bundle: list[dict[str, object]] = [
        {"agent_id": "ok", "artifact_type": "prompt"},
        "not-a-dict",  # type: ignore[list-item]
    ]
    _seed_bundle_job(db_session, "RFN-BAD", bundle)
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-BAD/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["targets"]) == 1
    assert data["targets"][0]["agent_id"] == "ok"


def test_get_job_targets_handles_null_field_values(client: TestClient, db_session: Session) -> None:
    """``{"agent_id": None, ...}`` must fall back to the job's
    primary agent_id rather than serializing as ``"None"``
    (deep-review Finding 6)."""
    bundle: list[dict[str, object]] = [
        {"agent_id": None, "artifact_type": "prompt"},
        {"agent_id": "real-agent", "artifact_type": None},
    ]
    _seed_bundle_job(db_session, "RFN-NULLS", bundle)
    response = client.get("/ajax-api/2.0/mlflow/caliber/jobs/RFN-NULLS/targets")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["targets"][0]["agent_id"] == "support-agent"  # job's primary
    assert data["targets"][0]["artifact_type"] == "prompt"
    assert data["targets"][1]["agent_id"] == "real-agent"
    assert data["targets"][1]["artifact_type"] == "prompt"  # falls back to job's
    # No row carries the literal string ``"None"``.
    assert all(t["agent_id"] != "None" for t in data["targets"])
    assert all(t["artifact_type"] != "None" for t in data["targets"])


# ---------------------------------------------------------------------------
# GET /jobs, /jobs/{id} — live GEPA progress
# ---------------------------------------------------------------------------
#
# GEPA can spend minutes inside a single ``candidate`` stage call with the job
# row itself never changing, so the UI has nothing to show but a static
# "running". ``_gepa_progress`` fills that gap by reading the ``eval_score``
# metric GEPA already logs to the job's own MLflow run mid-flight (see
# ``caliber.llm.openai_agents._generate_candidate_gepa`` and
# ``mlflow.genai.optimize.optimize.prompt_optimization_autolog``, which reuses
# the active run rather than starting a nested one). These tests fake the
# MLflow client rather than requiring a real tracking store.


class _FakeMetric:
    def __init__(self, step: int, value: float, timestamp: int) -> None:
        self.step = step
        self.value = value
        self.timestamp = timestamp


class _FakeMlflowClient:
    def __init__(self, history: list[_FakeMetric]) -> None:
        self._history = history

    def get_metric_history(self, run_id: str, key: str) -> list[_FakeMetric]:
        assert key == "eval_score"
        return self._history


def _fake_mlflow_module(history: list[_FakeMetric]) -> types.ModuleType:
    mod = types.ModuleType("mlflow")
    mod.MlflowClient = lambda: _FakeMlflowClient(history)  # type: ignore[attr-defined]
    return mod


def _seed_gepa_job(
    session: Session,
    job_id: str = "RFN-GEPA-LIVE",
    *,
    status: str = "running",
    stage: str = "candidate",
    optimizer_type: str = "GEPA",
    mlflow_run_id: str | None = "run-gepa-1",
) -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-GEPA-LIVE",
            agent_id="support-agent",
            category="quality",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-GEPA-LIVE",
            artifact_type="prompt",
            optimizer_type=optimizer_type,
            status=status,
            current_stage=stage,
            mlflow_run_id=mlflow_run_id,
            bundle_targets=[],
        )
    )
    session.commit()


def test_get_job_includes_gepa_progress_for_running_candidate_stage(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _seed_gepa_job(db_session)
    # Deliberately out of order -- the endpoint must sort by step, not
    # assume ``get_metric_history`` returns them in logging order.
    history = [
        _FakeMetric(step=2, value=0.81, timestamp=3_000),
        _FakeMetric(step=0, value=0.40, timestamp=1_000),
        _FakeMetric(step=1, value=0.55, timestamp=2_000),
    ]
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: _fake_mlflow_module(history))

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-GEPA-LIVE"))
    assert response.status_code == 200
    progress = response.json()["data"]["gepa_progress"]
    assert progress is not None
    assert progress["iterations"] == 3
    assert progress["latest_step"] == 2
    assert progress["latest_score"] == 0.81
    assert progress["history"] == [
        {"step": 0, "score": 0.40},
        {"step": 1, "score": 0.55},
        {"step": 2, "score": 0.81},
    ]
    assert progress["updated_at"] == "1970-01-01T00:00:03+00:00"


def test_list_jobs_includes_gepa_progress(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _seed_gepa_job(db_session)
    history = [_FakeMetric(step=0, value=0.5, timestamp=1_000)]
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: _fake_mlflow_module(history))

    response = client.get(LIST_PATH)
    assert response.status_code == 200
    (row,) = [item for item in response.json()["data"] if item["job_id"] == "RFN-GEPA-LIVE"]
    assert row["gepa_progress"] == {
        "iterations": 1,
        "latest_step": 0,
        "latest_score": 0.5,
        "updated_at": "1970-01-01T00:00:01+00:00",
        "history": [{"step": 0, "score": 0.5}],
    }


def test_gepa_progress_none_for_non_gepa_optimizer(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _seed_gepa_job(db_session, job_id="RFN-METAPROMPT", optimizer_type="MetaPrompt")
    history = [_FakeMetric(step=0, value=0.5, timestamp=1_000)]
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: _fake_mlflow_module(history))

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-METAPROMPT"))
    assert response.status_code == 200
    assert response.json()["data"]["gepa_progress"] is None


def test_gepa_progress_none_once_candidate_stage_is_done(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Once the job leaves ``running/candidate`` the stale progress readout
    should disappear rather than freezing on the last-logged iteration."""
    _seed_gepa_job(db_session, job_id="RFN-GEPA-DONE", status="running", stage="eval")
    history = [_FakeMetric(step=0, value=0.5, timestamp=1_000)]
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: _fake_mlflow_module(history))

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-GEPA-DONE"))
    assert response.status_code == 200
    assert response.json()["data"]["gepa_progress"] is None


def test_gepa_progress_none_when_mlflow_unavailable(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _seed_gepa_job(db_session, job_id="RFN-GEPA-NOMLFLOW")
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: None)

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-GEPA-NOMLFLOW"))
    assert response.status_code == 200
    assert response.json()["data"]["gepa_progress"] is None


def test_gepa_progress_none_without_run_id(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Before the worker binds an MLflow run there's nothing to read yet --
    degrade to ``None`` rather than raising on a null run id."""
    _seed_gepa_job(db_session, job_id="RFN-GEPA-NORUN", mlflow_run_id=None)
    history = [_FakeMetric(step=0, value=0.5, timestamp=1_000)]
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: _fake_mlflow_module(history))

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-GEPA-NORUN"))
    assert response.status_code == 200
    assert response.json()["data"]["gepa_progress"] is None


def test_gepa_progress_none_when_no_metrics_logged_yet(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """GEPA hasn't completed its first full-validation pass yet."""
    _seed_gepa_job(db_session, job_id="RFN-GEPA-EMPTY")
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: _fake_mlflow_module([]))

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-GEPA-EMPTY"))
    assert response.status_code == 200
    assert response.json()["data"]["gepa_progress"] is None


def test_gepa_progress_none_when_mlflow_client_raises(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """A flaky/unreachable tracking store degrades to no-progress, not a 500 --
    this is a UI nicety, never allowed to break the job endpoint."""
    _seed_gepa_job(db_session, job_id="RFN-GEPA-BOOM")

    class _RaisingClient:
        def get_metric_history(self, run_id: str, key: str) -> list[object]:
            raise RuntimeError("tracking store unreachable")

    mod = types.ModuleType("mlflow")
    mod.MlflowClient = lambda: _RaisingClient()  # type: ignore[attr-defined]
    monkeypatch.setattr(jobs_routes, "_get_mlflow_module", lambda: mod)

    response = client.get(DETAIL_PATH.replace("{job_id}", "RFN-GEPA-BOOM"))
    assert response.status_code == 200
    assert response.json()["data"]["gepa_progress"] is None


# ---------------------------------------------------------------------------
# POST /jobs/{id}/request-changes
# ---------------------------------------------------------------------------


def _seed_candidate_ready_job(session: Session, job_id: str = "RFN-RC") -> None:
    session.add(
        CaliberAgentConfig(
            agent_id="support-agent",
            experiment_id="exp",
            name="Support",
            owner="@sarah",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-RC",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
            status="verified",
        )
    )
    session.flush()
    session.add(
        CaliberRefinementJob(
            job_id=job_id,
            agent_id="support-agent",
            primary_item_id="FB-RC",
            artifact_type="prompt",
            status="candidate_ready",
            current_stage="done",
            refine_iteration=2,
            bundle_targets=[],
            candidate={"content": "rewritten prompt body", "artifact_type": "prompt"},
        )
    )
    session.commit()


def test_request_changes_returns_job_to_running_at_candidate_stage(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        REQUEST_CHANGES_PATH.replace("{job_id}", "RFN-RC"),
        json={"notes": "please cite the refund policy"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]["job"]
    assert data["status"] == "running"
    assert data["current_stage"] == "candidate"

    db_session.expire_all()
    job = db_session.get(CaliberRefinementJob, "RFN-RC")
    assert job is not None
    assert job.review_notes == "please cite the refund policy"
    # A deliberate human action, distinct from the automatic self-correction
    # budget: it must not consume/reset refine_iteration.
    assert job.refine_iteration == 2


def test_request_changes_requires_notes(client: TestClient, db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(REQUEST_CHANGES_PATH.replace("{job_id}", "RFN-RC"), json={})
    assert response.status_code == 400


def test_request_changes_on_non_candidate_ready_job_returns_409(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-RC")
    assert job is not None
    job.status = "running"
    db_session.commit()

    response = client.post(
        REQUEST_CHANGES_PATH.replace("{job_id}", "RFN-RC"),
        json={"notes": "please cite the refund policy"},
    )
    assert response.status_code == 409


def test_request_changes_missing_job_returns_404(client: TestClient) -> None:
    response = client.post(
        REQUEST_CHANGES_PATH.replace("{job_id}", "RFN-NONE"),
        json={"notes": "please cite the refund policy"},
    )
    assert response.status_code == 404


def test_request_changes_requires_operator_scope(
    client: TestClient, db_session: Session
) -> None:
    _seed_candidate_ready_job(db_session)
    response = client.post(
        REQUEST_CHANGES_PATH.replace("{job_id}", "RFN-RC"),
        json={"notes": "please cite the refund policy"},
        headers={"X-CALIBER-User": ""},
    )
    assert response.status_code in (401, 403)
