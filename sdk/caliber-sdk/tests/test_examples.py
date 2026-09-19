"""Every published example is executed here.

The SDK plan requires documentation snippets to come from tested code rather
than hand-written prose. That only holds if something runs them, so each
example is exercised against a stub server that answers the calls it makes.

Two failure modes this catches that a prose snippet cannot: an example calling
a method that no longer exists, and an example whose request shape drifted from
what the server accepts.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient
from examples.agentic import install_ready_cookbook, plan_from_intent
from examples.evaluation import build_and_score
from examples.openapi_integration_governed_write import import_and_publish_governed_write_tool
from examples.openapi_integration_readonly import import_and_publish_readonly_tool
from examples.prompt_lifecycle import prompt_lifecycle
from examples.quickstart import quickstart
from examples.tokens import issue_scoped_token
from examples.verification_queue import flag_and_verify
from examples.workflow_bundle import clone_sealed_release
from examples.workflow_deployment import promote_and_rollback, record_gate_verdict
from examples.workflow_run import run_and_wait
from examples.workspace_github_source import connect_github_source_and_import_on_push
from examples.workspace_release import promote_a_reviewed_revision

BASE = "https://caliber.test"


def stub_server(routes: dict[str, Any]) -> CaliberClient:
    """A client whose responses are keyed by ``METHOD /path`` suffix."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        key = f"{request.method} {path}"
        if key not in routes:  # pragma: no cover - surfaces as a test failure
            raise AssertionError(f"example called an unstubbed route: {key}")
        body = routes[key]
        payload = body(request) if callable(body) else body
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json={"data": payload})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def test_quickstart_reports_identity_and_ga_surfaces() -> None:
    caliber = stub_server(
        {
            "GET /me": {"user_id": "@alice", "scopes": ["caliber.admin"], "is_admin": True},
            "GET /capabilities": {
                "sdk_stability": {"ga": ["prompts", "workflows"]},
                "workflow_runs": {"queue_enabled": True},
            },
        }
    )
    with caliber:
        result = quickstart(caliber)

    assert result["user_id"] == "@alice"
    assert result["ga_surfaces"] == ["prompts", "workflows"]
    assert result["queue_enabled"] is True


def test_quickstart_exits_when_the_credential_is_not_usable() -> None:
    """The example must handle the anonymous answer, not assume an exception."""
    caliber = stub_server({"GET /me": {"user_id": "anonymous", "scopes": []}})
    with caliber, pytest.raises(SystemExit):
        quickstart(caliber)


def test_token_example_issues_scoped_then_revokes() -> None:
    seen: list[str] = []

    def record(request: httpx.Request) -> Any:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        if request.method == "POST":
            body = jsonlib.loads(request.content)
            assert body["scopes"] == ["caliber.operator"]
            return {"token_id": "PAT-1", "name": "ci", "token": "calpat_secret"}
        return {"tokens": [{"token_id": "PAT-1", "active": True}]}

    caliber = stub_server(
        {
            "POST /auth/tokens": record,
            "GET /auth/tokens": record,
            "DELETE /auth/tokens/PAT-1": {"token_id": "PAT-1", "revoked": True},
        }
    )
    with caliber:
        result = issue_scoped_token(caliber)

    assert result["token_id"] == "PAT-1"
    assert result["live_before_revoke"] == ["PAT-1"]


def test_prompt_lifecycle_keeps_authoring_and_promotion_separate() -> None:
    """The property the example exists to demonstrate."""
    seen: list[str] = []

    def record(request: httpx.Request) -> Any:
        seen.append(request.url.path.rsplit("/caliber", 1)[-1])
        return {"version": 2}

    caliber = stub_server(
        {
            "POST /prompts": record,
            "POST /prompts/intake-classifier/versions": record,
            "POST /prompts/intake-classifier/aliases/prod": record,
        }
    )
    with caliber:
        result = prompt_lifecycle(caliber)

    assert result["promoted_version"] == 2
    # Registering a version and promoting it are two distinct calls.
    assert seen == [
        "/prompts",
        "/prompts/intake-classifier/versions",
        "/prompts/intake-classifier/aliases/prod",
    ]


def test_evaluation_example_uses_a_judge_with_an_evaluation_variable() -> None:
    """A judge with no ``{{ variable }}`` grades nothing; the server rejects it."""

    def judge(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        assert "{{ inputs }}" in body["instructions"] or "{{ outputs }}" in body["instructions"]
        return {"judge_id": "J-1", "feedback_value_type": "bool"}

    def add_example(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        # The server's field is ``input`` (singular); an ``inputs=`` call used
        # to pass every mocked test here while 422ing against a real server.
        assert "input" in body and "inputs" not in body
        return {"example_id": "EX-1"}

    def evaluations_create(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        # A judge is selected by name in ``scorers``; a bare ``judge_id`` field
        # does not exist on the request schema and would 422 for real.
        assert body["scorers"] == ["Judge.J-1"]
        assert "judge_id" not in body
        return {"evaluation_id": "EV-1", "status": "queued"}

    caliber = stub_server(
        {
            "POST /eval-datasets": {"dataset_id": "ED-1"},
            "POST /eval-datasets/ED-1/examples": add_example,
            "POST /judges": judge,
            "POST /evaluations": evaluations_create,
        }
    )
    with caliber:
        result = build_and_score(caliber)

    assert result == {"dataset_id": "ED-1", "judge_id": "J-1", "evaluation_id": "EV-1"}


def test_verification_queue_example_lists_before_verifying() -> None:
    """A different step lists and decides — not the same click that flagged it."""

    def verify(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        assert body["verification_notes"] == "Confirmed against the source doc."
        return {
            "item": {"item_id": "FB-1", "status": "verified", "verified_by": "@you"},
            "job": None,
        }

    caliber = stub_server(
        {
            "POST /verification-queue": {
                "item_id": "FB-1",
                "agent_id": "support-agent",
                "status": "pending",
            },
            "GET /verification-queue": [{"item_id": "FB-1", "status": "pending"}],
            "POST /verification-queue/FB-1/verify": verify,
        }
    )
    with caliber:
        result = flag_and_verify(caliber)

    assert result == {"item_id": "FB-1", "status": "verified"}


def test_workflow_example_targets_an_alias_and_waits() -> None:
    states = iter(["queued", "succeeded"])

    def submit(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        # Targeting the alias is the point: the caller does not change when a
        # new version is promoted.
        assert body["alias"] == "prod"
        assert body["idempotency_key"] == "example-run-1"
        return {"workflow_run_id": "WR-1", "status": "queued"}

    caliber = stub_server(
        {
            "POST /workflow-runs": submit,
            "GET /workflow-runs/WR-1": lambda _r: {
                "workflow_run_id": "WR-1",
                "status": next(states),
                "output": {"answer": "30 days"},
            },
        }
    )
    with caliber:
        result = run_and_wait(caliber, workflow_id="WF-1")

    assert result["status"] == "succeeded"
    assert result["output"] == {"answer": "30 days"}


def test_workflow_example_reports_a_failed_run_rather_than_crashing() -> None:
    """`WorkflowRunFailed` carries the run, so the example can say *why*."""
    caliber = stub_server(
        {
            "POST /workflow-runs": {"workflow_run_id": "WR-1", "status": "queued"},
            "GET /workflow-runs/WR-1": {"workflow_run_id": "WR-1", "status": "failed"},
        }
    )
    with caliber:
        result = run_and_wait(caliber, workflow_id="WF-1")

    assert result == {"run_id": "WR-1", "status": "failed"}


def test_workflow_bundle_example_verifies_before_importing() -> None:
    bundle = {"kind": "caliber.workflow_deployment_bundle", "schema_version": 1}

    def assert_bundle(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        assert body == {"deployment_bundle": bundle, "name": "Portable copy"}
        if request.url.path.endswith("/preview"):
            return {"ready_to_import": True}
        return {
            "workflow": {
                "workflow_id": "WF-copy",
                "name": "Portable copy",
                "project_id": None,
                "description": "",
                "owner": "@test",
                "status": "active",
                "default_experiment_id": None,
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-01T00:00:00Z",
            },
            "version": {"version_id": "WFV-copy"},
        }

    caliber = stub_server(
        {
            "GET /workflow-versions/WFV-1/deployment-bundle/status": {
                "valid": True,
                "ready_to_deploy": True,
                "digest": "abc",
            },
            "GET /workflow-versions/WFV-1/export/deployment-bundle": httpx.Response(
                200, content=jsonlib.dumps(bundle).encode()
            ),
            "POST /workflows/import/preview": assert_bundle,
            "POST /workflows/import": assert_bundle,
        }
    )
    with caliber:
        result = clone_sealed_release(
            caliber,
            version_id="WFV-1",
            new_name="Portable copy",
        )

    assert result == {
        "source_digest": "abc",
        "workflow_id": "WF-copy",
        "name": "Portable copy",
    }


def test_deployment_example_promotes_twice_then_rolls_back_to_the_first() -> None:
    """Rollback undoes the *last* promotion, so the example promotes the
    presumed-live version first -- giving the alias a checkpoint -- before
    promoting the candidate it then rolls back."""
    live: dict[str, str] = {}

    def promote(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        live["version_id"] = body["version_id"]
        return {"deployment_id": "DEP-1", "alias": "staging", "version_id": body["version_id"]}

    def rollback(_request: httpx.Request) -> Any:
        # The server pops its own checkpoint stack; this stub just reports
        # whichever version the *first* promote call recorded.
        return {"deployment_id": "DEP-1", "alias": "staging", "version_id": "WFV-1"}

    caliber = stub_server(
        {
            "POST /workflows/WF-1/deployments/staging/promote": promote,
            "GET /workflows/WF-1/deployments": lambda _r: [
                {"deployment_id": "DEP-1", "alias": "staging", "version_id": live["version_id"]}
            ],
            "POST /workflows/WF-1/deployments/staging/rollback": rollback,
        }
    )
    with caliber:
        result = promote_and_rollback(
            caliber, workflow_id="WF-1", live_version_id="WFV-1", candidate_version_id="WFV-2"
        )

    assert result == {"promoted_to": "WFV-2", "restored_to": "WFV-1"}


def test_gate_verdict_example_records_a_pass_or_fail_state() -> None:
    def handler(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        return {"state": body["state"]}

    caliber = stub_server({"POST /gate-verdicts/workflow/WFV-1": handler})
    with caliber:
        passed = record_gate_verdict(caliber, version_id="WFV-1", passed=True)
        failed = record_gate_verdict(caliber, version_id="WFV-1", passed=False)

    assert passed == {"state": "pass"}
    assert failed == {"state": "fail"}


# --- agentic examples ------------------------------------------------------


def test_plan_example_approves_only_when_the_plan_asks() -> None:
    """The human decision stays explicit rather than implied by continuing."""
    states = iter(["planning", "paused"])
    seen: list[str] = []

    def record(request: httpx.Request) -> Any:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        return {
            "plan": {"plan_id": "PLAN-1", "goal": "g", "status": "completed"},
            "steps": [{"step_id": "STEP-1", "plan_id": "PLAN-1", "title": "Inspect"}],
        }

    caliber = stub_server(
        {
            "POST /aria/plans": record,
            "GET /aria/plans/PLAN-1": lambda _r: {
                "plan": {"plan_id": "PLAN-1", "goal": "g", "status": next(states)},
                "steps": [{"step_id": "STEP-1", "plan_id": "PLAN-1", "title": "Inspect"}],
            },
            "POST /aria/plans/PLAN-1/approve": record,
            "POST /aria/plans/PLAN-1/execute": record,
        }
    )
    with caliber:
        result = plan_from_intent(caliber, "create a judge and a test set")

    assert result["status"] == "completed"
    assert "POST /aria/plans/PLAN-1/approve" in seen
    assert "POST /aria/plans/PLAN-1/execute" in seen


def test_cookbook_example_reports_blockers_instead_of_failing() -> None:
    """Readiness is checked before installing, not discovered by a 400."""
    caliber = stub_server(
        {
            "GET /cookbooks": {
                "recipes": [
                    {
                        "id": "03",
                        "title": "Policy-Safe",
                        "readiness": {
                            "status": "configuration_required",
                            "checks": [
                                {"label": "Runtime approvals", "status": "configuration_required"}
                            ],
                        },
                    }
                ]
            }
        }
    )
    with caliber:
        result = install_ready_cookbook(caliber)

    assert result["installed"] is None
    assert result["blocked_by"] == {"03": ["Runtime approvals"]}


def test_cookbook_example_installs_a_ready_recipe_paused() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": {
                "recipes": [
                    {"id": "02", "title": "Precision Skills", "readiness": {"status": "ready"}}
                ]
            },
            "POST /cookbooks/02/install": {"workflow": {"status": "paused"}},
        }
    )
    with caliber:
        result = install_ready_cookbook(caliber)

    assert result == {"installed": "02", "workflow_status": "paused"}


# --- Workspace release example ---------------------------------------------


def test_workspace_release_example_runs_the_full_governed_path() -> None:
    """Change Request, evaluation, quality signoff, approval, and apply --
    every step a separate, typed, auditable call."""
    seen: list[str] = []

    def responds_with(payload: Any) -> Any:
        def handler(request: httpx.Request) -> Any:
            seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
            return payload

        return handler

    _operation = {
        "operation_id": "WRO-1",
        "project_id": "PRJ-1",
        "workspace_release_id": "WRL-1",
        "environment_id": "development",
        "kind": "apply",
        "idempotency_key": "apply-WRL-1",
        "expected_environment_lock_version": 1,
        "lock_version": 1,
        "requested_by": "@you",
        "observation_count": 0,
    }
    _evaluation = {
        "evaluation_id": "WRE-1",
        "project_id": "PRJ-1",
        "workspace_release_id": "WRL-1",
        "idempotency_key": "eval-WRL-1",
        "evaluation_plan_sha256": "d" * 64,
        "input_sha256": "e" * 64,
        "status": "succeeded",
        "attempt_number": 1,
        "requested_by": "@you",
    }
    _decision = {
        "workspace_release_id": "WRL-1",
        "decision": "go",
        "rationale": "",
        "revision_sha256": "g" * 64,
        "environment_config_sha256": "a" * 64,
        "runtime_dependencies_sha256": "b" * 64,
        "gate_evidence_sha256": "f" * 64,
        "policy_sha256": "c" * 64,
    }

    caliber = stub_server(
        {
            "POST /projects/PRJ-1/change-requests": responds_with(
                {
                    "change_request_id": "WCR-1",
                    "project_id": "PRJ-1",
                    "current_head_revision_id": "WSR-1",
                    "created_by": "@you",
                    "title": "Tighten the intake prompt",
                    "status": "draft",
                    "lock_version": 1,
                    "current_head": {"head_id": "WCH-1", "revision_id": "WSR-1"},
                }
            ),
            "POST /projects/PRJ-1/change-requests/WCR-1:submit": responds_with(
                {"change_request_id": "WCR-1", "project_id": "PRJ-1", "status": "open"}
            ),
            "POST /projects/PRJ-1/releases": responds_with(
                {
                    "release_id": "WRL-1",
                    "project_id": "PRJ-1",
                    "revision_id": "WSR-1",
                    "environment_id": "development",
                    "environment_config_sha256": "a" * 64,
                    "runtime_dependencies_sha256": "b" * 64,
                    "policy_sha256": "c" * 64,
                    "request_idempotency_key": "release-WSR-1",
                    "status": "draft",
                    "requested_by": "@you",
                    "lock_version": 1,
                }
            ),
            "POST /projects/PRJ-1/releases/WRL-1/evaluate": responds_with(_evaluation),
            "GET /projects/PRJ-1/releases/WRL-1/evaluations/WRE-1": responds_with(_evaluation),
            "POST /projects/PRJ-1/releases/WRL-1/quality-signoff": responds_with(
                {**_decision, "decision_id": "WRD-1", "kind": "quality", "decided_by": "@qa"}
            ),
            "POST /projects/PRJ-1/releases/WRL-1/approve": responds_with(
                {**_decision, "decision_id": "WRD-2", "kind": "release", "decided_by": "@owner"}
            ),
            "POST /projects/PRJ-1/releases/WRL-1/operations": responds_with(
                {"operation": {**_operation, "status": "prepared"}, "items": []}
            ),
            "POST /projects/PRJ-1/releases/WRL-1/operations/WRO-1:apply": responds_with(
                {"operation": {**_operation, "status": "applied"}, "items": []}
            ),
            "GET /projects/PRJ-1/releases/WRL-1/operations/WRO-1": responds_with(
                {"operation": {**_operation, "status": "applied"}, "items": []}
            ),
        }
    )
    with caliber:
        result = promote_a_reviewed_revision(caliber)

    assert result == {
        "change_request_id": "WCR-1",
        "release_id": "WRL-1",
        "evaluation_status": "succeeded",
        "approval_decision": "go",
        "operation_status": "applied",
    }
    # Every governance stage is its own call -- none of them merged together.
    assert seen == [
        "POST /projects/PRJ-1/change-requests",
        "POST /projects/PRJ-1/change-requests/WCR-1:submit",
        "POST /projects/PRJ-1/releases",
        "POST /projects/PRJ-1/releases/WRL-1/evaluate",
        "GET /projects/PRJ-1/releases/WRL-1/evaluations/WRE-1",
        "POST /projects/PRJ-1/releases/WRL-1/quality-signoff",
        "POST /projects/PRJ-1/releases/WRL-1/approve",
        "POST /projects/PRJ-1/releases/WRL-1/operations",
        "POST /projects/PRJ-1/releases/WRL-1/operations/WRO-1:apply",
        "GET /projects/PRJ-1/releases/WRL-1/operations/WRO-1",
    ]


# --- GitHub-backed Workspace source example ---------------------------------


def test_github_source_example_configures_then_imports_on_push() -> None:
    """Connection setup and the push import are two independent typed calls --
    the same distinction the example's own docstring makes."""
    seen: list[str] = []

    def responds_with(payload: Any, *, status_code: int = 200) -> Any:
        def handler(request: httpx.Request) -> Any:
            seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
            return httpx.Response(status_code, json={"data": payload})

        return handler

    caliber = stub_server(
        {
            "PUT /projects/PRJ-1/source/connection": responds_with(
                {
                    "connection": {
                        "connection_id": "WSC-1",
                        "source_id": "SRC-1",
                        "project_id": "PRJ-1",
                        "provider": "github",
                        "app_id": "123456",
                        "installation_id": "789012",
                        "status": "active",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                }
            ),
            "POST /projects/PRJ-1/revision-imports": responds_with(
                {
                    "import_job_id": "WSI-1",
                    "project_id": "PRJ-1",
                    "source_id": "SRC-1",
                    "repository": "octo-org/mortgage-underwriting",
                    "commit_sha": "a" * 40,
                    "status": "queued",
                    "idempotency_key": f"push-{'a' * 40}",
                    "attempt_count": 1,
                    "max_attempts": 5,
                    "created_by": "@ci",
                },
                status_code=202,
            ),
            # The waiter always re-reads the job rather than trusting the
            # create() response, so a single already-terminal GET is enough --
            # no sleep, no second poll.
            "GET /projects/PRJ-1/revision-imports/WSI-1": responds_with(
                {
                    "import_job_id": "WSI-1",
                    "project_id": "PRJ-1",
                    "source_id": "SRC-1",
                    "repository": "octo-org/mortgage-underwriting",
                    "commit_sha": "a" * 40,
                    "status": "succeeded",
                    "revision_id": "WSR-1",
                    "idempotency_key": f"push-{'a' * 40}",
                    "attempt_count": 1,
                    "max_attempts": 5,
                    "created_by": "@ci",
                }
            ),
        }
    )
    with caliber:
        result = connect_github_source_and_import_on_push(caliber)

    assert result == {
        "connection_id": "WSC-1",
        "connection_status": "active",
        "import_job_id": "WSI-1",
        "import_status": "succeeded",
        "revision_id": "WSR-1",
    }
    # Configuring the connection and submitting the import are independent
    # typed calls -- neither is folded into the other.
    assert seen == [
        "PUT /projects/PRJ-1/source/connection",
        "POST /projects/PRJ-1/revision-imports",
        "GET /projects/PRJ-1/revision-imports/WSI-1",
    ]


# --- OpenAPI integration examples -----------------------------------------


def test_readonly_openapi_example_publishes_the_selected_operation() -> None:
    caliber = stub_server(
        {
            "POST /openapi-integrations": {"integration_id": "OAI-1"},
            "POST /openapi-integrations/OAI-1/import": {"version_id": "OAIV-1"},
            "GET /openapi-integrations/OAI-1/operations": [
                {
                    "operation_id": "OAOP-1",
                    "operation_key": "GET /incidents/{incident_id}",
                    "side_effect_level": "read",
                }
            ],
            "POST /openapi-integrations/OAI-1/tool-drafts/generate": [{"draft_id": "OATD-1"}],
            "POST /openapi-integrations/OAI-1/tool-drafts/OATD-1/preview": {
                "result": {"status_code": 200}
            },
            "POST /openapi-integrations/OAI-1/tool-drafts/OATD-1/publish": {
                "tool": {"tool_id": "TL-1", "name": "status"}
            },
        }
    )
    with caliber:
        result = import_and_publish_readonly_tool(caliber)

    assert result["tool_id"] == "TL-1"
    assert result["preview_status_code"] == 200


def test_governed_write_openapi_example_preserves_preview_gate() -> None:
    caliber = stub_server(
        {
            "POST /openapi-integrations": {"integration_id": "OAI-2"},
            "POST /openapi-integrations/OAI-2/import": {"version_id": "OAIV-2"},
            "GET /openapi-integrations/OAI-2/operations": [
                {
                    "operation_id": "OAOP-2",
                    "operation_key": "POST /tickets",
                    "side_effect_level": "write",
                }
            ],
            "POST /openapi-integrations/OAI-2/tool-drafts/generate": [
                {"draft_id": "OATD-2", "requires_approval": True}
            ],
            "POST /openapi-integrations/OAI-2/tool-drafts/OATD-2/preview": httpx.Response(
                409, json={"detail": "preview is not allowed"}
            ),
            "POST /openapi-integrations/OAI-2/tool-drafts/OATD-2/publish": {
                "draft": {"requires_approval": True},
                "tool": {
                    "tool_id": "TL-2",
                    "name": "create_ticket",
                    "side_effect_level": "write",
                },
            },
        }
    )
    with caliber:
        result = import_and_publish_governed_write_tool(caliber)

    assert result["tool_id"] == "TL-2"
    assert result["requires_approval"] is True
