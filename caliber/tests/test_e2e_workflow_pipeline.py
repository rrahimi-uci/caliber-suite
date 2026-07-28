"""End-to-end workflow lifecycle through the HTTP API (plan §19.15).

Exercises the full author → validate → compile → preview → publish → deploy →
rollback path with no mocks (uses the in-process fake executor). The CALIBER
refinement loop integration (verification → workflow patch → approval) builds on
this foundation in a later phase.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    deploy_prod,
    make_support_manifest,
    make_tool_payload,
    seed_eval_dataset,
)


class TestE2EWorkflowPipeline:
    def test_full_workflow_lifecycle(
        self, client: TestClient, db_session: Session, gated_prod: None
    ) -> None:
        # Exercises the full multi-stage governance lifecycle (deploy gate → prod
        # promotion → human approval → rollback); ``gated_prod`` restores that path
        # over the v1 single-environment default.
        seed_eval_dataset(db_session)

        # 1. Register tools.
        for name, kwargs in [
            ("lookup_policy", {"allow_in_preview": True}),
            ("get_order", {"allow_in_preview": True}),
            ("escalate", {"side_effect_level": "external_action"}),
        ]:
            r = client.post(f"{PREFIX}/tools", json=make_tool_payload(name, **kwargs))
            assert r.status_code == 201, r.text

        # 2. Create workflow.
        r = client.post(f"{PREFIX}/workflows", json={"name": "Support", "owner": "@test"})
        assert r.status_code == 201
        workflow_id = r.json()["data"]["workflow_id"]

        # 3. Create draft version with a prod deploy gate.
        manifest = make_support_manifest(
            workflow_id,
            deploy_gates={
                "support_eval": {
                    "type": "deploy_gate",
                    "dataset_ref": "support_eval",
                    "required_for_aliases": ["prod"],
                    # Completion, not quality: the seeded dataset carries no
                    # expected output, and this test's subject is the full
                    # lifecycle. Graded gating: test_deploy_gate_evidence.py.
                    "thresholds": {"min_completion_rate": 1.0},
                }
            },
        )
        r = client.post(f"{PREFIX}/workflows/{workflow_id}/versions", json={"manifest": manifest})
        assert r.status_code == 201
        version_id = r.json()["data"]["version_id"]

        # 4. Validate.
        r = client.post(f"{PREFIX}/workflow-versions/{version_id}/validate")
        assert r.status_code == 200 and r.json()["data"]["valid"] is True

        # 5. Compile.
        r = client.post(f"{PREFIX}/workflow-versions/{version_id}/compile")
        assert r.status_code == 200
        assert r.json()["data"]["compiled_artifact_uri"] is not None

        # 6. Preview run.
        r = client.post(
            f"{PREFIX}/workflow-versions/{version_id}/preview-run",
            json={"input": "What is your refund policy?"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "completed"

        # 7. Publish.
        r = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
        assert r.status_code == 200 and r.json()["data"]["status"] == "published"

        # 8. Deploy to dev (ungated).
        r = client.post(
            f"{PREFIX}/workflows/{workflow_id}/deployments/dev/promote",
            json={"version_id": version_id},
        )
        assert r.status_code == 200

        # 9. Promote to prod (gated) — deploy gate replays the eval dataset,
        #    then a reviewer approves the pending promotion before rotation.
        r = client.post(
            f"{PREFIX}/workflows/{workflow_id}/deployments/prod/promote",
            json={"version_id": version_id},
        )
        assert r.status_code == 202, r.text
        assert r.json()["data"]["rotated"] is False
        promotion_id = r.json()["data"]["promotion"]["promotion_id"]
        r = client.post(f"{PREFIX}/workflow-promotions/{promotion_id}/approve")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["deployment"]["alias"] == "prod"

        # 10. Publish a second version, deploy it to prod (promote + approve),
        #     then rollback prod to the first version.
        v2 = client.post(
            f"{PREFIX}/workflows/{workflow_id}/versions",
            json={
                "manifest": make_support_manifest(
                    workflow_id, name="Support v2", deploy_gates=manifest["deploy_gates"]
                )
            },
        ).json()["data"]["version_id"]
        client.post(f"{PREFIX}/workflow-versions/{v2}/publish")
        deploy_prod(client, workflow_id, v2)
        r = client.post(f"{PREFIX}/workflows/{workflow_id}/deployments/prod/rollback")
        assert r.status_code == 200
        assert r.json()["data"]["version_id"] == version_id

        # 11. Audit trail recorded the key mutations.
        actions = {row.action for row in db_session.query(CaliberAuditLog).all()}
        assert {
            "create_workflow",
            "publish_workflow_version",
            "request_workflow_promotion",
            "approve_workflow_promotion",
            "rollback_workflow",
        } <= actions

    def test_prod_deploy_requires_gate(self, client: TestClient, gated_prod: None) -> None:
        # With prod gated (multi-stage governance), promoting without a deploy gate
        # is rejected. The v1 single-environment default (ungated) is covered by
        # test_promote_prod_rotates_immediately_single_env.
        wid, vid = create_and_publish(client)  # support manifest has no gate
        r = client.post(
            f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid}
        )
        assert r.status_code == 400
        assert "deploy gate" in r.json()["detail"].lower()
