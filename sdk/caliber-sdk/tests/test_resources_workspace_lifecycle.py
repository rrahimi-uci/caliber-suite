"""Workspace source/import/revision lifecycle: ``ProjectSourceAPI``/
``ProjectImportsAPI``/``ProjectRevisionsAPI`` (`P6-B`).

Import/revision list endpoints are cursor-paginated, not offset-paginated
like the rest of this SDK -- these tests pin the request shape (``limit``/
``cursor`` params, ``next_cursor`` in the response) as much as the decoding.
Source mutations are optimistic-concurrency-checked with an ``If-Match``
etag -- these tests pin that every mutating call actually sends one when
given, and sends none when omitted (the create-vs-replace distinction the
server itself enforces).
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import CursorPage, decode
from caliber_sdk.models.workspace import (
    WorkspaceImportJob,
    WorkspaceImportReconciliation,
    WorkspaceRevision,
    WorkspaceSource,
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

_SOURCE: dict[str, Any] = {
    "source_id": "SRC-1",
    "project_id": "PRJ-1",
    "provider": "github",
    "provider_host": "github.com",
    "canonical_repository_id": "123456",
    "display_path": "org/repo",
    "default_branch": "main",
    "root_path": "",
    "manifest_path": ".caliber/workspace.yaml",
    "import_mode": "push",
    "status": "disabled",
    "external_review_policy_version": "v1",
    "etag": "etag-1",
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


# --- source: get / configure / enable / disable / reconcile / capabilities --


def test_source_get_decodes_a_configured_binding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/projects/PRJ-1/source")
        return envelope({"source_mode": "git_managed", "source": _SOURCE})

    with client_with(handler) as caliber:
        state = caliber.workspaces.source.get("PRJ-1")

    assert state.source_mode == "git_managed"
    assert isinstance(state.source, WorkspaceSource)
    assert state.source.etag == "etag-1"


def test_source_get_with_no_binding_decodes_a_null_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"source_mode": "caliber_managed", "source": None})

    with client_with(handler) as caliber:
        state = caliber.workspaces.source.get("PRJ-1")

    assert state.source_mode == "caliber_managed"
    assert state.source is None


def test_source_configure_first_time_sends_no_if_match_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_match"] = request.headers.get("If-Match")
        seen["body"] = jsonlib.loads(request.content)
        return envelope({"source_mode": "git_managed", "source": _SOURCE})

    with client_with(handler) as caliber:
        state = caliber.workspaces.source.configure(
            "PRJ-1",
            provider="github",
            provider_host="github.com",
            canonical_repository_id="123456",
            display_path="org/repo",
        )

    assert seen["if_match"] is None
    assert seen["body"]["provider"] == "github"
    assert seen["body"]["default_branch"] == "main"
    assert "connection_ref" not in seen["body"]
    assert state.source is not None and state.source.source_id == "SRC-1"


def test_source_configure_replacement_quotes_the_if_match_etag() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["if_match"] = request.headers.get("If-Match")
        return envelope({"source_mode": "git_managed", "source": _SOURCE})

    with client_with(handler) as caliber:
        caliber.workspaces.source.configure(
            "PRJ-1",
            provider="github",
            provider_host="github.com",
            canonical_repository_id="123456",
            display_path="org/repo",
            if_match="etag-0",
        )

    assert seen["if_match"] == '"etag-0"'


def test_source_enable_disable_reconcile_send_the_action_and_if_match() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path.rsplit("/caliber", 1)[-1],
                "method": request.method,
                "if_match": request.headers.get("If-Match"),
            }
        )
        return envelope({"source_mode": "git_managed", "source": _SOURCE})

    with client_with(handler) as caliber:
        caliber.workspaces.source.enable("PRJ-1", if_match="etag-1")
        caliber.workspaces.source.disable("PRJ-1", if_match="etag-1")
        caliber.workspaces.source.reconcile("PRJ-1", if_match="etag-1")

    assert seen == [
        {
            "path": "/projects/PRJ-1/source:enable",
            "method": "POST",
            "if_match": '"etag-1"',
        },
        {
            "path": "/projects/PRJ-1/source:disable",
            "method": "POST",
            "if_match": '"etag-1"',
        },
        {
            "path": "/projects/PRJ-1/source:reconcile",
            "method": "POST",
            "if_match": '"etag-1"',
        },
    ]


def test_source_capabilities_decodes_the_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/source/capabilities")
        return envelope(
            {
                "source_id": "SRC-1",
                "provider": "github",
                "provider_host": "github.com",
                "available": True,
                "capabilities": {"provider_pull": True},
            }
        )

    with client_with(handler) as caliber:
        caps = caliber.workspaces.source.capabilities("PRJ-1")

    assert caps.available is True
    assert caps.capabilities == {"provider_pull": True}


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


def test_revisions_snapshot_posts_the_explicit_pin_list_and_decodes_a_managed_revision() -> None:
    seen: dict[str, Any] = {}
    managed_revision = {
        **_REVISION,
        "revision_id": "WSR-managed-1",
        "source_kind": "managed",
        "manifest_sha256": None,
        "source_bundle_sha256": None,
        "resources": [
            {
                "resource_pin_id": "WSRR-managed-1",
                "revision_id": "WSR-managed-1",
                "resource_type": "prompt",
                "logical_name": "support-agent",
                "resource_id": "support-agent",
                "version_ref": "3",
                "content_sha256": "f" * 64,
                "provider_ref": "prompts:/support-agent/3",
                "purpose": "runtime",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["method"] = request.method
        seen["body"] = jsonlib.loads(request.content)
        return envelope(managed_revision)

    resources = [{"resource_type": "prompt", "resource_id": "support-agent", "version_ref": "3"}]
    with client_with(handler) as caliber:
        revision = caliber.workspaces.revisions.snapshot("PRJ-1", resources=resources)

    assert seen["method"] == "POST"
    assert seen["path"] == "/projects/PRJ-1/revisions:snapshot"
    assert seen["body"] == {"resources": resources}
    assert isinstance(revision, WorkspaceRevision)
    assert revision.revision_id == "WSR-managed-1"
    assert revision.source_kind == "managed"
    assert revision.manifest_sha256 is None
    assert revision.source_bundle_sha256 is None
    assert revision.resources[0].resource_type == "prompt"


# --- forward compatibility ---------------------------------------------------


def test_unknown_import_job_fields_are_kept_rather_than_dropped() -> None:
    job = decode(WorkspaceImportJob, {**_IMPORT_JOB, "a_field_from_the_future": 42})
    assert job.extra == {"a_field_from_the_future": 42}
