"""The installed Lab 22 example replayed against a deterministic fake server."""

from __future__ import annotations

import json

import httpx

from caliber_sdk import CaliberClient
from examples.cookbooks import cookbook_22_governed_evidence_review as lab

BASE = "https://caliber.test"


class FakeLab22:
    def __init__(self) -> None:
        self.actor = ""
        self.run_status = "waiting_approval"
        self.approved = False
        self.calls: list[str] = []

    def client(self, token: str) -> CaliberClient:
        return CaliberClient(
            BASE,
            token=token,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda request: self.handle(request, token))
            ),
        )

    def handle(self, request: httpx.Request, token: str) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else {}

        def ok(data: object, status: int = 200) -> httpx.Response:
            return httpx.Response(status, json={"data": data})

        if (request.method, path) == ("POST", "/projects"):
            return ok({"project_id": "PRJ-22", "name": body["name"]}, 201)
        if path == "/projects/PRJ-22/members" and request.method == "POST":
            return ok(
                {"project_id": "PRJ-22", "user_id": body["user_id"], "role": body["role"]},
                201,
            )
        if (request.method, path) == ("GET", "/cookbooks"):
            return ok({"recipes": [{"id": "22", "title": "Governed Evidence Review", "steps": []}]})
        if (request.method, path) == ("POST", "/cookbooks/22/install"):
            return ok(
                {
                    "workflow": {"workflow_id": "WF-22", "status": "paused"},
                    "version": {"version_id": "WFV-22", "status": "draft"},
                },
                201,
            )
        if (request.method, path) == ("PATCH", "/workflows/WF-22"):
            return ok({"workflow_id": "WF-22", "name": "Lab 22", "status": "active"})
        if (request.method, path) == ("POST", "/workflow-versions/WFV-22/publish"):
            return ok({"version_id": "WFV-22", "workflow_id": "WF-22", "status": "published"})
        if (request.method, path) == ("POST", "/workflow-runs"):
            return ok(
                {
                    "workflow_run_id": "RUN-22",
                    "workflow_id": "WF-22",
                    "workflow_version_id": "WFV-22",
                    "status": "waiting_approval",
                },
                202,
            )
        if (request.method, path) == ("GET", "/workflow-runs/RUN-22"):
            return ok(
                {
                    "workflow_run_id": "RUN-22",
                    "workflow_id": "WF-22",
                    "status": self.run_status,
                    "acceptance": (
                        {"status": "accepted"} if self.run_status == "completed" else None
                    ),
                }
            )
        if (request.method, path) == ("GET", "/workflow-runs/RUN-22/approvals"):
            return ok(
                [
                    {
                        "runtime_approval_id": "RAP-22",
                        "workflow_run_id": "RUN-22",
                        "project_id": "PRJ-22",
                        "node_id": "request",
                        "status": "approved" if self.approved else "pending",
                        "proposal_id": "PROP-22",
                    }
                ]
            )
        if (request.method, path) == ("GET", "/workflow-runs/RUN-22/approvals/RAP-22"):
            return ok(
                {
                    "approval": {
                        "runtime_approval_id": "RAP-22",
                        "workflow_run_id": "RUN-22",
                        "project_id": "PRJ-22",
                        "node_id": "request",
                        "status": "pending",
                    },
                    "binding": {
                        "proposal_id": "PROP-22",
                        "intent_hash": "sha256:intent",
                        "evidence_hash": "sha256:evidence",
                        "policy_digest": "sha256:policy",
                        "expires_at": "2026-11-04T00:00:00Z",
                    },
                    "proposal": {
                        "proposal_id": "PROP-22",
                        "connector": "document-request",
                        "connector_version": "1",
                        "effect_class": "irreversible_write",
                        "resource": f"cases/{lab.CASE_ID}/document-requests",
                        "state": "proposed",
                        "args_fields": [{"name": "document_type", "kind": "string"}],
                        "args_field_count": 1,
                    },
                    "policy": {"required_role": "caliber.approver", "approval_count": 1},
                    "quorum": {"approvers": [], "required": 1, "remaining": 1},
                    "state": {"code": "open"},
                    "allowed_responses": ["approve", "reject", "request_changes"],
                    "caller": {
                        "principal": "editor",
                        "approve": {"allowed": token == "tok-approver"},
                        "reject": {"allowed": token == "tok-approver"},
                    },
                }
            )
        if (request.method, path) == ("POST", "/workflow-runs/RUN-22/approval/approve"):
            if token != "tok-approver":
                return httpx.Response(403, json={"detail": "project role required"})
            assert body == {
                "runtime_approval_id": "RAP-22",
                "reason": "the exact synthetic request is authorized",
            }
            self.approved = True
            return ok({"workflow_run_id": "RUN-22", "status": "waiting_approval"})
        if (request.method, path) == ("POST", "/workflow-runs/RUN-22/resume"):
            assert self.approved
            self.run_status = "completed"
            return ok({"workflow_run_id": "RUN-22", "status": "queued"}, 202)
        if (request.method, path) == ("GET", "/workflow-runs/RUN-22/causal-links"):
            return ok(
                {
                    "workflow_run_id": "RUN-22",
                    "section": "effects",
                    "effects": [
                        {
                            "proposal_id": "PROP-22",
                            "approval": {"status": "linked", "approval_id": "RAP-22"},
                            "effect": {
                                "status": "linked",
                                "effect_id": "EFF-22",
                                "committed": True,
                            },
                        }
                    ],
                }
            )
        if (request.method, path) == ("GET", "/workflow-runs/RUN-22/evidence"):
            return httpx.Response(200, content=b"PK\x03\x04deterministic-archive")
        if (request.method, path) == ("POST", "/workflow-runs/RUN-22/replay"):
            return ok(
                {
                    "schema_version": "v1",
                    "workflow_run_id": "RUN-22",
                    "manifest_id": "MF-22",
                    "manifest_digest": "d" * 64,
                    "observation_count": 1,
                    "observation_ids": ["OBS-22"],
                    "criteria_version": "harborlight-v1",
                    "decisions": [],
                    "matches_original": True,
                }
            )
        raise AssertionError(f"unstubbed route {request.method} {path}")


def test_lab22_sdk_example_uses_typed_run_scoped_journey() -> None:
    fake = FakeLab22()
    result = lab.run(fake.client("tok-admin"), approver=fake.client("tok-approver"), suffix="-test")

    assert result["workflow_run_id"] == "RUN-22"
    assert result["proposal_id"] == "PROP-22"
    assert result["runtime_approval_id"] == "RAP-22"
    assert result["effect_id"] == "EFF-22"
    assert result["replay_matches_original"] is True
    assert "POST /workflow-runs/RUN-22/approval/approve" in fake.calls
    assert "POST /workflow-runs/RUN-22/replay" in fake.calls
