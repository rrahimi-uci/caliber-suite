"""End-to-end workflow calibration loop through the API and worker."""

from __future__ import annotations

import time

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from tests.workflow_helpers import (
    PREFIX,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
    seed_eval_dataset,
)


def test_workflow_calibration_run_reaches_approval_and_promotes(
    worker_client: TestClient,
    db_session: Session,
) -> None:
    agent = worker_client.post(
        f"{PREFIX}/agents",
        json={
            "agent_id": "support-agent",
            "experiment_id": "exp-support-calibration-e2e",
            "name": "Support Agent",
            "owner": "@test",
        },
    )
    assert agent.status_code in (201, 409), agent.text
    register_demo_tools(worker_client)
    seed_eval_dataset(db_session)

    workflow_id = create_workflow(worker_client, "Support Calibration E2E")
    manifest = make_support_manifest(
        workflow_id,
        deploy_gates={
            "support_eval_gate": {
                "type": "deploy_gate",
                "dataset_ref": "support_eval",
                "required_for_aliases": ["dev"],
                "thresholds": {},
            }
        },
    )
    version = worker_client.post(
        f"{PREFIX}/workflows/{workflow_id}/versions", json={"manifest": manifest}
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["data"]["version_id"]
    publish = worker_client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert publish.status_code == 200, publish.text
    deploy = worker_client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/dev/promote",
        json={"version_id": version_id},
    )
    assert deploy.status_code == 200, deploy.text

    run = worker_client.post(
        f"{PREFIX}/workflows/{workflow_id}/calibration/runs",
        json={
            "agent_id": "support-agent",
            "objective": {"maximize": "tool_adherence", "epsilon": 0.0},
            "budget": {"max_candidates": 2, "max_eval_examples": 20, "min_examples": 1},
        },
    )
    assert run.status_code == 201, run.text
    job_id = run.json()["data"]["job"]["job_id"]

    deadline = time.monotonic() + 20
    status = None
    while time.monotonic() < deadline:
        detail = worker_client.get(f"{PREFIX}/jobs/{job_id}")
        assert detail.status_code == 200, detail.text
        status = detail.json()["data"]["status"]
        if status in ("candidate_ready", "applied", "completed", "rejected", "failed"):
            break
        time.sleep(0.3)
    assert status == "candidate_ready", f"unexpected job status {status}"

    job_detail = worker_client.get(f"{PREFIX}/jobs/{job_id}").json()["data"]
    assert job_detail["candidate"]["calibration"] is True
    assert job_detail["candidate"]["calibration_winner_id"]
    assert job_detail["eval_results"]["calibration"] is True

    patches = worker_client.get(f"{PREFIX}/workflows/{workflow_id}/patches")
    assert patches.status_code == 200, patches.text
    patch_rows = [row for row in patches.json()["data"] if row["job_id"] == job_id]
    assert len(patch_rows) == 1

    applied = worker_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
    assert applied.status_code == 200, applied.text
    promotion = applied.json()["data"]["promotion"]
    assert promotion is not None
    new_version_id = promotion["artifact_ref"]

    deployments = worker_client.get(f"{PREFIX}/workflows/{workflow_id}/deployments")
    assert deployments.status_code == 200, deployments.text
    dev = next(row for row in deployments.json()["data"] if row["alias"] == "dev")
    assert dev["version_id"] == new_version_id
    assert new_version_id != version_id
