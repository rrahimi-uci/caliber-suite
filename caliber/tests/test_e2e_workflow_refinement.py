"""End-to-end CALIBER workflow-refinement loop (plan §12.2, §17).

Exercises the *automated* loop through the real worker: a flagged workflow
trace → a verified feedback item with workflow context → a queued
``workflow_manifest`` refinement job → the worker auto-runs workflow diagnosis
→ candidate (semantic patch) → eval (compile + replay) → candidate_ready. The
operator Apply endpoint then publishes a new workflow version and rotates the
alias.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberRefinementJob, CaliberVerificationItem
from caliber.ids import new_item_id, new_job_id
from tests.workflow_helpers import (
    PREFIX,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
)


class TestE2EWorkflowRefinement:
    def test_flagged_trace_auto_produces_and_promotes_a_workflow_patch(
        self, worker_client: TestClient, db_session: Session
    ) -> None:
        # 1. Register the CALIBER agent the feedback routes through + tools.
        r = worker_client.post(
            f"{PREFIX}/agents",
            json={
                "agent_id": "support-agent",
                "experiment_id": "exp-support-wf",
                "name": "Support Agent",
                "owner": "@test",
            },
        )
        assert r.status_code in (201, 409), r.text
        register_demo_tools(worker_client)

        # 2. Create + publish + deploy a baseline workflow (no grounding guard yet).
        workflow_id = create_workflow(worker_client, "Support Refund")
        manifest = make_support_manifest(workflow_id)
        # Drop the existing policy guardrail so the candidate clearly *adds* one.
        del manifest["nodes"]["policy_guardrail"]
        manifest["edges"] = [
            {
                "id": "e_start_support",
                "from": "start",
                "to": "support_agent",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_support_final",
                "from": "support_agent",
                "to": "final",
                "map": {"final_output": "response"},
            },
        ]
        r = worker_client.post(
            f"{PREFIX}/workflows/{workflow_id}/versions", json={"manifest": manifest}
        )
        assert r.status_code == 201, r.text
        version_id = r.json()["data"]["version_id"]
        r = worker_client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
        assert r.status_code == 200, r.text
        r = worker_client.post(
            f"{PREFIX}/workflows/{workflow_id}/deployments/dev/promote",
            json={"version_id": version_id},
        )
        assert r.status_code == 200, r.text

        # 3/4. A production trace is flagged (the agent answered a refund-policy
        #      question without calling lookup_policy) and verified, seeding a
        #      queued workflow_manifest refinement job directly — the human-
        #      feedback intake routes are gone; calibration / optimization seed
        #      jobs the same way. The worker picks the queued job up.
        item = CaliberVerificationItem(
            item_id=new_item_id(),
            agent_id="support-agent",
            category="tool_use",
            free_text="Agent answered the refund policy without calling lookup_policy.",
            severity="standard",
            status="verified",
            verified_by="@test",
            workflow_id=workflow_id,
            submitted_context={
                "workflow_version_id": version_id,
                "node_id": "support_agent",
                "required_tools": ["lookup_policy"],
                "observed_tool_calls": [],
            },
        )
        db_session.add(item)
        db_session.flush()
        job = CaliberRefinementJob(
            job_id=new_job_id(),
            agent_id="support-agent",
            workflow_id=workflow_id,
            primary_item_id=item.item_id,
            artifact_type="workflow_manifest",
            status="queued",
            current_stage="triage",
            bundle_targets=[],
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.job_id
        assert job.artifact_type == "workflow_manifest"

        # 5. The worker auto-advances diagnosis → candidate → eval and lands
        #    the job at the terminal candidate_ready state (no approval row).
        deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < deadline:
            status = worker_client.get(f"{PREFIX}/jobs/{job_id}").json()["data"]["status"]
            if status in ("candidate_ready", "applied", "completed", "rejected", "failed"):
                break
            time.sleep(0.3)
        assert status == "candidate_ready", f"unexpected status {status}"

        # 6. A workflow patch was persisted and shows the added guardrail.
        patches = worker_client.get(f"{PREFIX}/workflows/{workflow_id}/patches").json()["data"]
        assert len(patches) >= 1
        added = [n["id"] for n in (patches[0]["graph_diff"]["added_nodes"])]
        assert any(nid.endswith("grounding_guard") for nid in added)

        # 7. Apply → publishes a new workflow version + rotates the dev alias.
        r = worker_client.post(f"{PREFIX}/jobs/{job_id}/apply", json={})
        assert r.status_code == 200, r.text
        promotion = r.json()["data"]["promotion"]
        assert promotion is not None
        new_version_id = promotion["artifact_ref"]

        # 8. A second published version now exists and dev points at it.
        versions = worker_client.get(f"{PREFIX}/workflows/{workflow_id}/versions").json()["data"]
        published = [v for v in versions if v["status"] == "published"]
        assert len(published) >= 2
        deployments = worker_client.get(f"{PREFIX}/workflows/{workflow_id}/deployments").json()[
            "data"
        ]
        dev = next(d for d in deployments if d["alias"] == "dev")
        assert dev["version_id"] == new_version_id

        # 9. The new version's manifest contains the grounding guardrail.
        new_manifest = worker_client.get(f"{PREFIX}/workflow-versions/{new_version_id}").json()[
            "data"
        ]["manifest"]
        assert any(nid.endswith("grounding_guard") for nid in new_manifest["nodes"])

        # 10. Job applied.
        assert worker_client.get(f"{PREFIX}/jobs/{job_id}").json()["data"]["status"] == "applied"
