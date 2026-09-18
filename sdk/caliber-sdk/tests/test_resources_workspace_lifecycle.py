"""Workspace import/revision lifecycle: ``ProjectImportsAPI``/``ProjectRevisionsAPI`` (`P6-B`).

Import/revision list endpoints are cursor-paginated, not offset-paginated
like the rest of this SDK -- these tests pin the request shape (``limit``/
``cursor`` params, ``next_cursor`` in the response) as much as the decoding.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import CursorPage, decode
from caliber_sdk.models.workspace import (
    WorkspaceImportJob,
    WorkspaceImportReconciliation,
    WorkspaceRevision,
)

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, *, next_cursor: str | None = "unset") -> httpx.Response:
    """A single-object envelope, or a list-page envelope when ``next_cursor`` is passed."""
    if next_cursor == "unset":
        return httpx.Response(200, json={"data": data})
    return httpx.Response(200, json={"data": data, "next_cursor": next_cursor})


_IMPORT_JOB: dict[str, Any] = {
    "import_job_id": "WSI-1",
    "project_id": "PRJ-1",
    "source_id": "SRC-1",
    "repository": "org/repo",
    "commit_sha": "a" * 40,
    "status": "queued",
    "idempotency_key": "idem-1",
    "attempt_count": 0,
    "max_attempts": 3,
    "created_by": "user-1",
}

_REVISION_RESOURCE: dict[str, Any] = {
    "resource_pin_id": "WSRR-1",
    "revision_id": "WSR-1",
    "resource_type": "workflow",
    "logical_name": "triage",
    "resource_id": "WF-1",
    "version_ref": "WFV-1",
    "content_sha256": "e" * 64,
    "purpose": "primary",
}

_REVISION: dict[str, Any] = {
    "revision_id": "WSR-1",
    "project_id": "PRJ-1",
    "revision_number": 1,
    "manifest_sha256": "b" * 64,
    "source_bundle_sha256": "c" * 64,
    "source_attestation": "caller_attested",
    "revision_sha256": "d" * 64,
    "status": "ready",
    "created_by": "user-1",
    "resources": [_REVISION_RESOURCE],
}


# --- CursorPage --------------------------------------------------------------


def test_cursor_page_has_more_reflects_next_cursor() -> None:
    assert CursorPage(items=[1], next_cursor="tok").has_more
    assert not CursorPage(items=[1], next_cursor=None).has_more


# --- imports: list / get / create / reconcile / wait ------------------------


def test_imports_list_sends_limit_cursor_and_status_and_decodes_the_page() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope({"items": [_IMPORT_JOB]}, next_cursor="cursor-2")

    with client_with(handler) as caliber:
        page = caliber.workspaces.imports.list(
            "PRJ-1", status="queued", limit=25, cursor="cursor-1"
        )

    assert seen["path"] == "/projects/PRJ-1/revision-imports"
    assert seen["params"] == {"status": "queued", "limit": "25", "cursor": "cursor-1"}
    assert page.next_cursor == "cursor-2"
    assert page.has_more
    assert page.items == [decode(WorkspaceImportJob, _IMPORT_JOB)]


def test_imports_list_with_no_more_pages_has_a_null_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"items": []}, next_cursor=None)

    with client_with(handler) as caliber:
        page = caliber.workspaces.imports.list("PRJ-1")

    assert page.items == []
    assert page.next_cursor is None
    assert not page.has_more


def test_imports_get_hits_the_detail_path_and_decodes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/revision-imports/WSI-1")
        return envelope(_IMPORT_JOB)

    with client_with(handler) as caliber:
        job = caliber.workspaces.imports.get("PRJ-1", "WSI-1")

    assert job.import_job_id == "WSI-1"
    assert job.status == "queued"
    assert not job.is_terminal


def test_imports_create_sends_multipart_with_idempotency_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["idempotency_key"] = request.headers.get("Idempotency-Key")
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        return httpx.Response(202, json={"data": {**_IMPORT_JOB, "status": "queued"}})

    with client_with(handler) as caliber:
        job = caliber.workspaces.imports.create(
            "PRJ-1",
            repository="org/repo",
            commit_sha="a" * 40,
            bundle=b"tar-bytes",
            idempotency_key="my-retry-key",
        )

    assert seen["content_type"].startswith("multipart/form-data")
    assert seen["idempotency_key"] == "my-retry-key"
    assert seen["path"] == "/projects/PRJ-1/revision-imports"
    assert job.import_job_id == "WSI-1"


def test_imports_reconcile_posts_the_reconcile_action_and_decodes_the_nested_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/revision-imports/WSI-1:reconcile")
        assert request.method == "POST"
        return envelope(
            {
                "job": {**_IMPORT_JOB, "status": "succeeded"},
                "observed": True,
                "observation": "found_on_disk",
            }
        )

    with client_with(handler) as caliber:
        result = caliber.workspaces.imports.reconcile("PRJ-1", "WSI-1")

    assert isinstance(result, WorkspaceImportReconciliation)
    assert result.observed is True
    assert result.observation == "found_on_disk"
    assert isinstance(result.job, WorkspaceImportJob)
    assert result.job.status == "succeeded"
    assert result.job.is_terminal


def test_imports_wait_polls_past_non_terminal_states() -> None:
    states = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({**_IMPORT_JOB, "status": next(states)})

    with client_with(handler) as caliber:
        job = caliber.workspaces.imports.wait(
            "PRJ-1", "WSI-1", interval=0.001, max_interval=0.001, timeout=5
        )

    assert job.status == "succeeded"


def test_imports_wait_treats_reconcile_required_as_terminal() -> None:
    """It will never advance on its own -- a waiter must not block past it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({**_IMPORT_JOB, "status": "reconcile_required"})

    with client_with(handler) as caliber:
        job = caliber.workspaces.imports.wait(
            "PRJ-1", "WSI-1", interval=0.001, max_interval=0.001, timeout=5
        )

    assert job.status == "reconcile_required"
    assert job.is_terminal


# --- revisions: list / get / diff -------------------------------------------


def test_revisions_list_sends_cursor_params_and_decodes_nested_resources() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope({"items": [_REVISION]}, next_cursor=None)

    with client_with(handler) as caliber:
        page = caliber.workspaces.revisions.list("PRJ-1", status="ready", limit=10)

    assert seen["path"] == "/projects/PRJ-1/revisions"
    assert seen["params"] == {"status": "ready", "limit": "10"}
    assert len(page.items) == 1
    revision = page.items[0]
    assert isinstance(revision, WorkspaceRevision)
    assert revision.revision_id == "WSR-1"
    assert len(revision.resources) == 1
    assert revision.resources[0].logical_name == "triage"


def test_revisions_get_decodes_nested_resources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/revisions/WSR-1")
        return envelope(_REVISION)

    with client_with(handler) as caliber:
        revision = caliber.workspaces.revisions.get("PRJ-1", "WSR-1")

    assert revision.revision_number == 1
    assert revision.resources[0].resource_id == "WF-1"


def test_revisions_diff_sends_base_query_param_and_decodes_change_sets() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope(
            {
                "base_revision_id": "WSR-1",
                "revision_id": "WSR-2",
                "manifest_changed": True,
                "source_bundle_changed": False,
                "source_commit_changed": False,
                "added": [_REVISION_RESOURCE],
                "removed": [],
                "changed": [],
            }
        )

    with client_with(handler) as caliber:
        diff = caliber.workspaces.revisions.diff("PRJ-1", "WSR-2", base="WSR-1")

    assert seen["path"] == "/projects/PRJ-1/revisions/WSR-2/diff"
    assert seen["params"] == {"base": "WSR-1"}
    assert diff.manifest_changed is True
    assert len(diff.added) == 1
    assert diff.removed == []


# --- forward compatibility ---------------------------------------------------


def test_unknown_import_job_fields_are_kept_rather_than_dropped() -> None:
    job = decode(WorkspaceImportJob, {**_IMPORT_JOB, "a_field_from_the_future": 42})
    assert job.extra == {"a_field_from_the_future": 42}
