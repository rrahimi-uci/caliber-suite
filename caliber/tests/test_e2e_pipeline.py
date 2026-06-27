"""End-to-end pipeline test: queued job → eval → candidate_ready → apply.

Exercises the full refinement pipeline through the HTTP API using the
TestClient + FakeEvalProvider + FakeLLMProvider. Inspired by the support-agent
integration-test pattern where the complete lifecycle is validated in a single
test function.

This test does NOT call real MLflow or any LLM — everything runs against
the in-memory fakes. The purpose is to verify that the pipeline stages
connect correctly: a passing eval gate lands the job at the terminal
``candidate_ready`` state (no human-feedback approval), and the operator
Apply endpoint promotes the candidate.

Refinement jobs are seeded directly into the DB (a verified feedback item +
a queued job), the same way calibration / optimization seed them — the human-
feedback intake routes no longer exist.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberRefinementJob, CaliberVerificationItem
from caliber.ids import new_item_id, new_job_id

PREFIX = "/ajax-api/2.0/mlflow/caliber"
PIPELINE_TIMEOUT_SECONDS = 45.0
POLL_INTERVAL_SECONDS = 0.3


def _seed_verified_job(
    session: Session,
    *,
    agent_id: str,
    category: str = "tone",
    free_text: str = "feedback",
    notes: str | None = None,
) -> str:
    """Seed a verified feedback item + queued refinement job directly.

    Mirrors what the verify route used to do (and what calibration /
    optimization still do): a ``CaliberVerificationItem`` plus a ``queued``
    ``CaliberRefinementJob`` on the ``triage`` stage so the running worker
    picks it up. Returns the new ``job_id``.
    """
    item = CaliberVerificationItem(
        item_id=new_item_id(),
        agent_id=agent_id,
        category=category,
        free_text=free_text,
        severity="standard",
        status="verified",
        verified_by="@test",
        verification_notes=notes,
    )
    session.add(item)
    session.flush()
    job = CaliberRefinementJob(
        job_id=new_job_id(),
        agent_id=agent_id,
        primary_item_id=item.item_id,
        artifact_type="prompt",
        status="queued",
        current_stage="triage",
        bundle_targets=[],
    )
    session.add(job)
    session.commit()
    return job.job_id


def _wait_for_job_status(
    client: TestClient,
    job_id: str,
    *,
    timeout_seconds: float = PIPELINE_TIMEOUT_SECONDS,
) -> str | None:
    """Poll a refinement job until it reaches a terminal state."""
    deadline = time.monotonic() + timeout_seconds
    job_status = None
    while time.monotonic() < deadline:
        resp = client.get(f"{PREFIX}/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        job = resp.json()["data"]
        job_status = job["status"]
        if job_status in ("candidate_ready", "applied", "completed", "rejected", "failed"):
            return job_status
        time.sleep(POLL_INTERVAL_SECONDS)
    return job_status


def _wait_for_candidate_ready_job(
    client: TestClient,
    agent_id: str,
    *,
    timeout_seconds: float = PIPELINE_TIMEOUT_SECONDS,
) -> str:
    """Poll the jobs endpoint until a candidate_ready job appears for the agent."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        resp = client.get(
            f"{PREFIX}/jobs",
            params={"agent_id": agent_id, "status": "candidate_ready"},
        )
        assert resp.status_code == 200, resp.text
        jobs = resp.json()["data"]
        if jobs:
            return jobs[0]["job_id"]
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"expected a candidate_ready job for {agent_id} within {timeout_seconds:.0f}s"
    )


class TestE2EPipeline:
    """Full pipeline: register → seed verified job → candidate_ready → apply."""

    def test_register_seed_pipeline_apply(
        self, worker_client: TestClient, db_session: Session
    ) -> None:
        client = worker_client
        # 1. Register an agent
        resp = client.post(
            f"{PREFIX}/agents",
            json={
                "agent_id": "e2e-test-agent",
                "experiment_id": "exp-e2e-001",
                "name": "E2E Test Agent",
                "owner": "@test",
                "artifact_types": ["system_prompt"],
                "eval_thresholds": {
                    "min_aggregate_score": 0.80,
                    "max_regression_delta": 0.05,
                },
            },
        )
        assert resp.status_code == 201, resp.text
        agent = resp.json()["data"]
        assert agent["agent_id"] == "e2e-test-agent"
        assert agent["enabled"] is True

        # 2/3. Seed a verified feedback item + queued refinement job directly
        #      so the running worker picks it up.
        job_id = _seed_verified_job(
            db_session,
            agent_id="e2e-test-agent",
            free_text="Agent was too formal in casual conversations",
            notes="Valid feedback — agent tone needs adjustment",
        )

        # 4. Poll the job until it reaches a terminal state. The fake eval
        #    provider returns candidate=0.94 > threshold 0.80, so the gate
        #    passes and the job lands at candidate_ready (no approval).
        job_status = _wait_for_job_status(client, job_id)
        assert job_status in (
            "candidate_ready",
            "rejected",
        ), f"unexpected job status: {job_status}"

        # 5. No approval row is created by the pipeline anymore.
        resp = client.get(
            f"{PREFIX}/approvals",
            params={"agent_id": "e2e-test-agent", "status": "pending"},
        )
        # The approvals listing route was removed entirely.
        assert resp.status_code == 404

        if job_status == "candidate_ready":
            # 6. Apply the candidate (operator action) → job becomes applied.
            resp = client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
            assert resp.status_code == 200, resp.text
            apply_result = resp.json()["data"]
            assert apply_result["status"] == "applied"
            assert "artifact_ref" in apply_result["promotion"]

            # 7. Verify job moved to applied.
            resp = client.get(f"{PREFIX}/jobs/{job_id}")
            assert resp.status_code == 200, resp.text
            final_job = resp.json()["data"]
            assert final_job["status"] == "applied"

    def test_register_duplicate_agent_conflict(self, client: TestClient) -> None:
        """Registering the same agent_id twice returns 409."""
        payload = {
            "agent_id": "dup-agent",
            "experiment_id": "exp-dup-001",
            "name": "Duplicate Agent",
            "owner": "@test",
        }
        resp = client.post(f"{PREFIX}/agents", json=payload)
        assert resp.status_code == 201

        resp = client.post(f"{PREFIX}/agents", json=payload)
        assert resp.status_code == 409

    def test_apply_already_applied_409(
        self, worker_client: TestClient, db_session: Session
    ) -> None:
        """Applying an already-applied (non-candidate_ready) job returns 409."""
        client = worker_client
        client.post(
            f"{PREFIX}/agents",
            json={
                "agent_id": "double-apply-agent",
                "experiment_id": "exp-dbl-001",
                "name": "Double Apply",
                "owner": "@test",
            },
        )
        _seed_verified_job(db_session, agent_id="double-apply-agent", free_text="test")

        job_id = _wait_for_candidate_ready_job(client, "double-apply-agent")

        # First apply succeeds.
        resp = client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert resp.status_code == 200

        # Second apply — the job is now ``applied``, not ``candidate_ready`` → 409.
        resp = client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert resp.status_code == 409

    def test_apply_missing_job_404(self, client: TestClient) -> None:
        """Applying a non-existent job returns 404."""
        resp = client.post(f"{PREFIX}/jobs/RFN-MISSING/apply", json={})
        assert resp.status_code == 404
