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
from examples.evaluation import build_and_score
from examples.prompt_lifecycle import prompt_lifecycle
from examples.quickstart import quickstart
from examples.tokens import issue_scoped_token
from examples.workflow_run import run_and_wait

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
            "POST /prompts/intake-classifier/promote": record,
        }
    )
    with caliber:
        result = prompt_lifecycle(caliber)

    assert result["promoted_version"] == 2
    # Registering a version and promoting it are two distinct calls.
    assert seen == [
        "/prompts",
        "/prompts/intake-classifier/versions",
        "/prompts/intake-classifier/promote",
    ]


def test_evaluation_example_uses_a_judge_with_an_evaluation_variable() -> None:
    """A judge with no ``{{ variable }}`` grades nothing; the server rejects it."""

    def judge(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        assert "{{ inputs }}" in body["instructions"] or "{{ outputs }}" in body["instructions"]
        return {"judge_id": "J-1", "feedback_value_type": "bool"}

    caliber = stub_server(
        {
            "POST /eval-datasets": {"dataset_id": "ED-1"},
            "POST /eval-datasets/ED-1/examples": {"example_id": "EX-1"},
            "POST /judges": judge,
            "POST /evaluations": {"evaluation_id": "EV-1", "status": "queued"},
        }
    )
    with caliber:
        result = build_and_score(caliber)

    assert result == {"dataset_id": "ED-1", "judge_id": "J-1", "evaluation_id": "EV-1"}


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
