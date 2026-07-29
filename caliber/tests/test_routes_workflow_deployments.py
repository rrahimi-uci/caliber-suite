"""Integration tests for workflow deployment routes (plan §19.9)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberMcpServer
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    create_draft,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
    relax_release_graded_executor,
    relax_release_quality_gate,
    seed_eval_dataset,
)

# ``gated_prod`` fixture (re-enables the dormant gated-promotion machinery for the
# multi-stage governance tests below) lives in tests/conftest.py.


def _gated_manifest(workflow_id: str, **gate_overrides) -> dict:
    gate = {
        "type": "deploy_gate",
        "dataset_ref": "support_eval",
        "required_for_aliases": ["prod"],
        # These tests exercise the promotion/approval lifecycle, not scoring, and
        # their dataset carries no expected output. ``min_completion_rate`` is the
        # honest assertion for that: ``min_pass_rate`` now means "completed AND met
        # the scorer threshold" and fails closed without graded data.
        "thresholds": {"min_completion_rate": 1.0},
    }
    gate.update(gate_overrides)
    return make_support_manifest(workflow_id, deploy_gates={"support_eval": gate})


def test_list_deployments_empty(client: TestClient) -> None:
    wid, _ = create_and_publish(client)
    r = client.get(f"{PREFIX}/workflows/{wid}/deployments")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_promote_to_dev(client: TestClient) -> None:
    wid, vid = create_and_publish(client)
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["rotated"] is True
    assert data["deployment"]["alias"] == "dev"
    assert data["deployment"]["version_id"] == vid


def test_promote_unpublished_400(client: TestClient) -> None:
    register_demo_tools(client)
    wid = create_workflow(client)
    vid, _ = create_draft(client, wid, make_support_manifest(wid))
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid})
    assert r.status_code == 400


def _mcp_manifest(workflow_id: str) -> dict:
    manifest = make_support_manifest(workflow_id)
    manifest["tools"]["docs"] = {
        "type": "mcp_tool",
        "server_id": "MCP-docs",
        "tool_name": "search_docs",
        "side_effect_level": "read",
    }
    return manifest


def test_promote_preflights_missing_mcp_dependency(client: TestClient) -> None:
    wid, vid = create_and_publish(
        client,
        workflow_name="Missing MCP",
        manifest=_mcp_manifest("missing_mcp"),
    )
    response = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid},
    )
    assert response.status_code == 400
    assert "MCP server 'MCP-docs' does not exist" in response.json()["detail"]


def test_promote_allows_ready_mcp_in_dev_but_requires_external_boundary_in_prod(
    client: TestClient,
    db_session: Session,
) -> None:
    db_session.add(
        CaliberMcpServer(
            server_id="MCP-docs",
            name="Docs",
            transport="stdio",
            command="${PYTHON}",
            args=["-m", "caliber.mcp_servers.db", "--mode", "relational"],
            status="active",
            discovered_tools=[{"name": "search_docs"}],
            tool_policies={
                "search_docs": {
                    "allowed": True,
                    "side_effect_level": "read",
                    "requires_approval": False,
                }
            },
            last_connected_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    wid, vid = create_and_publish(
        client,
        workflow_name="Ready MCP",
        manifest=_mcp_manifest("ready_mcp"),
    )
    dev = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid},
    )
    assert dev.status_code == 200, dev.text
    prod = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/prod/promote",
        json={"version_id": vid},
    )
    assert prod.status_code == 400
    assert "requires an external MCP isolation boundary" in prod.json()["detail"]


def test_promote_prod_without_gate_400(client: TestClient, gated_prod: None) -> None:
    wid, vid = create_and_publish(client)  # support manifest has no deploy gate
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    assert r.status_code == 400
    assert "deploy gate" in r.json()["detail"].lower()


def test_promote_prod_without_a_gate_is_refused_by_default(client: TestClient) -> None:
    """Shipped default: a production promotion needs graded evidence.

    Rotating the live alias onto a version no gate has scored is exactly the
    false-release-evidence defect, so it fails closed with an actionable message.
    """
    wid, vid = create_and_publish(client)  # no deploy gate
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    assert r.status_code == 400, r.text
    assert "requires a deploy gate" in r.json()["detail"]
    assert client.get(f"{PREFIX}/workflows/{wid}/deployments").json()["data"] == []


def test_promote_prod_rotates_immediately_single_env(client: TestClient) -> None:
    """With the quality-gate requirement relaxed, single-environment prod still
    rotates at once — no promotion-approval queue."""
    wid, vid = create_and_publish(client)  # no deploy gate
    relax_release_quality_gate(client)
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["rotated"] is True
    assert data["deployment"]["alias"] == "prod"
    assert data["deployment"]["version_id"] == vid
    # No pending promotion is created.
    promotions = client.get(f"{PREFIX}/workflows/{wid}/promotions").json()["data"]
    assert promotions == []


def test_promote_prod_with_passing_gate_creates_pending(
    client: TestClient, db_session: Session, gated_prod: None
) -> None:
    seed_eval_dataset(db_session)
    relax_release_graded_executor(client)
    wid, vid = create_and_publish(
        client, workflow_name="Gated", manifest=_gated_manifest("gated_wf")
    )
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    assert r.status_code == 202, r.text
    data = r.json()["data"]
    assert data["rotated"] is False
    assert data["promotion"]["status"] == "pending"
    # Alias is NOT rotated yet.
    deployments = client.get(f"{PREFIX}/workflows/{wid}/deployments").json()["data"]
    assert all(d["alias"] != "prod" for d in deployments)


def test_approve_prod_promotion_rotates(
    client: TestClient, db_session: Session, gated_prod: None
) -> None:
    seed_eval_dataset(db_session)
    relax_release_graded_executor(client)
    wid, vid = create_and_publish(
        client, workflow_name="GatedApprove", manifest=_gated_manifest("gated_approve_wf")
    )
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    promotion_id = r.json()["data"]["promotion"]["promotion_id"]
    r = client.post(f"{PREFIX}/workflow-promotions/{promotion_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deployment"]["alias"] == "prod"
    assert r.json()["data"]["deployment"]["version_id"] == vid
    deployments = client.get(f"{PREFIX}/workflows/{wid}/deployments").json()["data"]
    assert any(d["alias"] == "prod" and d["version_id"] == vid for d in deployments)


def test_reject_prod_promotion_does_not_rotate(
    client: TestClient, db_session: Session, gated_prod: None
) -> None:
    seed_eval_dataset(db_session)
    relax_release_graded_executor(client)
    wid, vid = create_and_publish(
        client, workflow_name="GatedReject", manifest=_gated_manifest("gated_reject_wf")
    )
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    promotion_id = r.json()["data"]["promotion"]["promotion_id"]
    r = client.post(
        f"{PREFIX}/workflow-promotions/{promotion_id}/reject", json={"reason": "not yet"}
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "rejected"
    deployments = client.get(f"{PREFIX}/workflows/{wid}/deployments").json()["data"]
    assert all(d["alias"] != "prod" for d in deployments)


def test_promote_prod_operator_forbidden(client: TestClient, db_session: Session) -> None:
    seed_eval_dataset(db_session)
    relax_release_graded_executor(client)
    wid, vid = create_and_publish(
        client, workflow_name="GatedRbac", manifest=_gated_manifest("gated_rbac_wf")
    )
    r = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/prod/promote",
        json={"version_id": vid},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_list_promotions(client: TestClient, db_session: Session, gated_prod: None) -> None:
    seed_eval_dataset(db_session)
    relax_release_graded_executor(client)
    wid, vid = create_and_publish(
        client, workflow_name="GatedList", manifest=_gated_manifest("gated_list_wf")
    )
    client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    r = client.get(f"{PREFIX}/workflows/{wid}/promotions")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
    assert r.json()["data"][0]["status"] == "pending"


def test_promote_prod_with_failing_gate_400(client: TestClient, db_session: Session) -> None:
    seed_eval_dataset(db_session)
    relax_release_graded_executor(client)
    # A guardrail that always blocks (the fake executor output always contains "processed").
    manifest = _gated_manifest("failgate_wf")
    manifest["nodes"]["policy_guardrail"]["checks"] = [
        {"forbid_substring": {"substring": "processed"}}
    ]
    wid, vid = create_and_publish(client, workflow_name="FailGate", manifest=manifest)
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/prod/promote", json={"version_id": vid})
    assert r.status_code == 400
    # Assert *why* it was refused. Several release policies now answer 400, so a bare
    # status check would keep passing if the gate stopped being evaluated at all.
    body = r.json()
    assert body["gate"]["passed"] is False, body
    assert body["gate"]["has_gate"] is True, body


def test_rollback(client: TestClient) -> None:
    wid, vid1 = create_and_publish(client)
    client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid1})
    # publish a second version and promote it
    vid2, _ = create_draft(client, wid, make_support_manifest(wid, name="Support v2"))
    client.post(f"{PREFIX}/workflow-versions/{vid2}/publish")
    client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid2})
    # now rollback dev -> should restore vid1
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/rollback")
    assert r.status_code == 200
    assert r.json()["data"]["version_id"] == vid1


def test_rollback_no_checkpoint_404(client: TestClient) -> None:
    wid, vid = create_and_publish(client)
    client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid})
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/rollback")
    assert r.status_code == 404


def test_promote_expected_version_id_guard(client: TestClient) -> None:
    """A stale ``expected_version_id`` blocks the promotion (409); a matching one
    lets it through — optimistic concurrency for the live alias."""
    wid, vid1 = create_and_publish(client)
    client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid1})
    vid2, _ = create_draft(client, wid, make_support_manifest(wid, name="Support v2"))
    client.post(f"{PREFIX}/workflow-versions/{vid2}/publish")

    # dev currently serves vid1; caller expects a different version -> conflict.
    stale = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid2, "expected_version_id": "WFV-stale"},
    )
    assert stale.status_code == 409

    # Caller's expectation matches the live version -> proceeds.
    ok = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid2, "expected_version_id": vid1},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["rotated"] is True


def test_promote_viewer_forbidden(client: TestClient) -> None:
    wid, vid = create_and_publish(client)
    r = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_rollback_viewer_forbidden(client: TestClient) -> None:
    """Rollback is operator-scoped (lowered from admin so promoters can undo);
    a viewer with no operator scope is still rejected before any state change."""
    wid, vid = create_and_publish(client)
    client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid})
    r = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/rollback",
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_list_deployments_missing_workflow_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows/WF-nonexistent/deployments")
    assert r.status_code == 404


def test_promote_missing_workflow_404(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/workflows/WF-nonexistent/deployments/dev/promote",
        json={"version_id": "VER-xxx"},
    )
    assert r.status_code == 404


def test_promote_missing_version_404(client: TestClient) -> None:
    wid, _ = create_and_publish(client)
    r = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": "VER-nonexistent"},
    )
    assert r.status_code == 404


def test_promote_version_wrong_workflow_404(client: TestClient) -> None:
    """Promoting a version that belongs to a different workflow returns 404."""
    register_demo_tools(client)
    wid_a = create_workflow(client, name="Workflow A")
    wid_b = create_workflow(client, name="Workflow B")
    _, _ = create_draft(client, wid_a, make_support_manifest(wid_a))
    vid_b, _ = create_draft(client, wid_b, make_support_manifest(wid_b))
    client.post(f"{PREFIX}/workflow-versions/{vid_b}/publish")
    r = client.post(
        f"{PREFIX}/workflows/{wid_a}/deployments/dev/promote",
        json={"version_id": vid_b},
    )
    assert r.status_code == 404


def test_rollback_missing_workflow_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflows/WF-nonexistent/deployments/dev/rollback")
    assert r.status_code == 404


def test_list_promotions_missing_workflow_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows/WF-nonexistent/promotions")
    assert r.status_code == 404
