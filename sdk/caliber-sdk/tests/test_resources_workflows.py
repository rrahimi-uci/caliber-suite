"""Workflow, version, run, and service resource modules."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient, WorkflowRunFailed
from caliber_sdk.models import WorkflowRun, decode

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def test_the_five_route_groups_are_one_resource_tree() -> None:
    """A caller thinks in workflows; the route split is server-side detail."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/caliber", 1)[-1])
        return envelope([])

    with client_with(handler) as caliber:
        caliber.workflows.list()
        caliber.workflows.versions.list("WF-1")
        caliber.workflows.runs.list("WF-1")
        caliber.workflows.services.get("WF-1")

    assert seen == [
        "/workflows",
        "/workflows/WF-1/versions",
        # Runs are listed under their workflow: /workflow-runs is POST-only.
        "/workflows/WF-1/runs",
        # Service *management* is a property of the workflow; /services is the
        # external invocation surface, authenticated differently.
        "/workflows/WF-1/service",
    ]


def test_run_listing_decodes_the_real_envelope_with_pagination_metadata() -> None:
    """Regression test: ``GET /workflows/{id}/runs`` carries pagination
    metadata alongside the list -- ``{"data": [...], "next_cursor": ...}`` --
    not the bare ``{"data": [...]}`` shape every other list envelope in this
    file's ``envelope()`` helper produces. Decoded against a real server this
    silently returned ``[]`` regardless of how many runs existed, because the
    transport's generic unwrap only strips a *bare* ``{"data": ...}`` dict and
    otherwise leaves the payload alone -- caught only by an end-to-end test
    (``caliber/tests/test_sdk_against_server.py``), since every mocked test
    of this method used an empty list, which looks identical whether or not
    the bug is present. This test uses the real shape with real content
    specifically so that can't happen again.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"workflow_run_id": "WR-1", "workflow_id": "WF-1", "status": "queued"},
                    {"workflow_run_id": "WR-2", "workflow_id": "WF-1", "status": "succeeded"},
                ],
                "next_cursor": None,
            },
        )

    with client_with(handler) as caliber:
        runs = caliber.workflows.runs.list("WF-1")

    assert [run.workflow_run_id for run in runs] == ["WR-1", "WR-2"]


def test_submitting_a_run_posts_to_the_collection_not_a_version_subpath() -> None:
    """The server accepts a version id *or* a workflow plus alias.

    Forcing a version-scoped path would make the alias route unreachable, and
    that path is how a deployed workflow is actually invoked.
    """
    import json as jsonlib

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["body"] = jsonlib.loads(request.content)
        return envelope({"workflow_run_id": "WR-1", "status": "queued"}, 201)

    with client_with(handler) as caliber:
        caliber.workflows.runs.submit(workflow_id="WF-1", alias="prod", input={"q": 1})

    assert seen["path"] == "/workflow-runs"
    assert seen["body"] == {"workflow_id": "WF-1", "alias": "prod", "input": {"q": 1}}


def test_an_idempotency_key_is_passed_through() -> None:
    """Submission is the one mutating call the SDK cannot retry on its own."""
    import json as jsonlib

    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(jsonlib.loads(request.content))
        return envelope({"workflow_run_id": "WR-1", "status": "queued"}, 201)

    with client_with(handler) as caliber:
        caliber.workflows.runs.submit(workflow_version_id="WFV-1", idempotency_key="abc")

    assert sent["idempotency_key"] == "abc"


def test_run_terminal_and_success_are_distinct() -> None:
    """A cancelled run is terminal but did not succeed."""
    assert decode(WorkflowRun, {"status": "succeeded"}).succeeded
    assert decode(WorkflowRun, {"status": "cancelled"}).is_terminal
    assert not decode(WorkflowRun, {"status": "cancelled"}).succeeded
    assert not decode(WorkflowRun, {"status": "running"}).is_terminal


def test_waiting_on_a_run_raises_on_failure_by_default() -> None:
    """Unlike calibration: a script whose run failed usually wants to stop."""
    states = iter(["queued", "running", "failed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "WR-1", "status": next(states)})

    with client_with(handler) as caliber, pytest.raises(WorkflowRunFailed) as caught:
        caliber.workflows.runs.wait("WR-1", interval=0.001, max_interval=0.001, timeout=5)

    assert caught.value.run.status == "failed"


def test_a_failed_run_can_be_inspected_instead_of_raised() -> None:
    states = iter(["running", "failed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "WR-1", "status": next(states)})

    with client_with(handler) as caliber:
        run = caliber.workflows.runs.wait(
            "WR-1", raise_on_failure=False, interval=0.001, max_interval=0.001, timeout=5
        )

    assert run.status == "failed"


def test_a_successful_run_returns_rather_than_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"workflow_run_id": "WR-1", "status": "succeeded", "output": {"ok": 1}})

    with client_with(handler) as caliber:
        run = caliber.workflows.runs.wait("WR-1", interval=0.001, timeout=5)

    assert run.succeeded
    assert run.output == {"ok": 1}


def test_a_draft_version_is_distinguishable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"version_id": "WFV-1", "status": "draft", "manifest": {"nodes": {}}})

    with client_with(handler) as caliber:
        version = caliber.workflows.versions.get("WFV-1")

    assert version.is_draft
    assert version.manifest == {"nodes": {}}


def test_unpublishing_a_service_reads_the_status_ack() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/workflows/WF-1/service")
        return envelope({"status": "unpublished"})

    with client_with(handler) as caliber:
        assert caliber.workflows.services.unpublish("WF-1") is True


def test_service_openapi_is_fetched_unenveloped() -> None:
    """The per-workflow document, like the management one, is not enveloped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"openapi": "3.0.0", "paths": {}})

    with client_with(handler) as caliber:
        document = caliber.workflows.services.openapi("WF-1")

    assert document["openapi"] == "3.0.0"
