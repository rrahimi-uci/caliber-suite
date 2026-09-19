"""Projects and their files — the workspace-scoping surface.

Every other resource can be scoped to a project via the ``X-CALIBER-Project``
header, which the client sets for you. This module manages the projects
themselves, the files they hold, and (`P6-B`) membership/ownership, the
Git-backed source binding, source-to-revision import/revision lifecycle,
Change Request review flow, and environment release lifecycle:
:class:`ProjectMembersAPI`, :class:`ProjectSourceAPI`,
:class:`ProjectImportsAPI`, :class:`ProjectRevisionsAPI`,
:class:`ProjectChangeRequestsAPI`, :class:`ProjectVersionTagsAPI`,
:class:`ProjectReleasesAPI` and :class:`ProjectReleaseOperationsAPI`, exposed
as ``ProjectsAPI.members``/``.source``/``.imports``/``.revisions``/
``.change_requests``/``.version_tags``/``.releases``/``.release_operations``.
``ProjectsAPI``'s own ``list_members``/``add_member``/``update_member``/
``remove_member``/``transfer_ownership`` remain as flat delegates to
``.members`` -- a root convenience, not a second implementation.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from typing import Any, BinaryIO, TypeVar

from ..models._decode import decode, decode_list
from ..models.common import CursorPage
from ..models.core import Project, ProjectFile, ProjectFolder, ProjectMember, WorkspaceEnvironment
from ..models.operations import ReworkTask
from ..models.workspace import (
    WorkspaceBreakGlassApplyResult,
    WorkspaceChangeRequest,
    WorkspaceChangeRequestCheck,
    WorkspaceChangeRequestComment,
    WorkspaceChangeRequestHead,
    WorkspaceChangeRequestReview,
    WorkspaceChangeRequestReviewer,
    WorkspaceExternalReviewAttestation,
    WorkspaceImportJob,
    WorkspaceImportReconciliation,
    WorkspaceRelease,
    WorkspaceReleaseDecision,
    WorkspaceReleaseEvaluation,
    WorkspaceReleaseEvidence,
    WorkspaceReleaseOperation,
    WorkspaceReleaseOperationItem,
    WorkspaceReleaseOperationResult,
    WorkspaceRevision,
    WorkspaceRevisionDiff,
    WorkspaceRevisionResource,
    WorkspaceSource,
    WorkspaceSourceCapabilities,
    WorkspaceSourceState,
    WorkspaceVersionTag,
)
from ..waiters import wait_for
from ._base import Resource

_List = list
_T = TypeVar("_T")


def _if_match_header(if_match: str | None) -> dict[str, str] | None:
    """An ``If-Match`` header from a plain etag string.

    Always quoted: the server's own ``ETag`` response header is quoted, and
    its ``If-Match`` parser tolerates a quoted or bare value identically
    (including a literal ``"*"``) -- so a caller who round-trips
    ``state.source.etag`` back into ``if_match`` never has to think about
    HTTP quoting.
    """
    return {"If-Match": f'"{if_match}"'} if if_match is not None else None


def _decode_source_state(payload: Any) -> WorkspaceSourceState:
    state = decode(WorkspaceSourceState, payload)
    source_payload = payload.get("source") if isinstance(payload, dict) else None
    state.source = decode(WorkspaceSource, source_payload) if source_payload is not None else None
    return state


def _decode_import_reconciliation(payload: Any) -> WorkspaceImportReconciliation:
    result = decode(WorkspaceImportReconciliation, payload)
    job_payload = payload.get("job") if isinstance(payload, dict) else None
    result.job = decode(WorkspaceImportJob, job_payload)
    return result


def _decode_revision(payload: Any) -> WorkspaceRevision:
    revision = decode(WorkspaceRevision, payload)
    revision.resources = decode_list(
        WorkspaceRevisionResource, payload.get("resources") if isinstance(payload, dict) else None
    )
    return revision


def _decode_revision_diff(payload: Any) -> WorkspaceRevisionDiff:
    diff = decode(WorkspaceRevisionDiff, payload)
    if isinstance(payload, dict):
        diff.added = decode_list(WorkspaceRevisionResource, payload.get("added"))
        diff.removed = decode_list(WorkspaceRevisionResource, payload.get("removed"))
        diff.changed = decode_list(WorkspaceRevisionResource, payload.get("changed"))
    return diff


def _decode_change_request(payload: Any) -> WorkspaceChangeRequest:
    request = decode(WorkspaceChangeRequest, payload)
    head_payload = payload.get("current_head") if isinstance(payload, dict) else None
    request.current_head = decode(WorkspaceChangeRequestHead, head_payload)
    return request


def _decode_operation_result(payload: Any) -> WorkspaceReleaseOperationResult:
    operation_payload = payload.get("operation") if isinstance(payload, dict) else None
    items_payload = payload.get("items") if isinstance(payload, dict) else None
    return WorkspaceReleaseOperationResult(
        operation=decode(WorkspaceReleaseOperation, operation_payload),
        items=decode_list(WorkspaceReleaseOperationItem, items_payload),
    )


def _offset_params(limit: int | None, offset: int | None, **extra: Any) -> dict[str, Any] | None:
    params: dict[str, Any] = {key: value for key, value in extra.items() if value is not None}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return params or None


def _decision_body(
    decision: str,
    gate_evidence_sha256: str,
    rationale: str,
    change_request_head_id: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "decision": decision,
        "gate_evidence_sha256": gate_evidence_sha256,
        "rationale": rationale,
    }
    if change_request_head_id is not None:
        body["change_request_head_id"] = change_request_head_id
    return body


def _page_params(limit: int | None, cursor: str | None) -> dict[str, Any] | None:
    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if cursor is not None:
        params["cursor"] = cursor
    return params or None


def _list_items_and_cursor(payload: Any) -> tuple[Any, str | None]:
    """A list endpoint's raw items plus its ``next_cursor``.

    Tolerates both wire shapes this SDK's cursor-paginated endpoints use:
    ``{"data": {"items": [...]}}`` (import/revision/change-request lists) and
    ``{"data": [...]}`` (change-request sub-resource and version-tag lists).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else data
    next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
    return items, next_cursor if isinstance(next_cursor, str) else None


def _cursor_page(item_type: type[_T], items: Any, next_cursor: str | None) -> CursorPage[_T]:
    return CursorPage(items=decode_list(item_type, items), next_cursor=next_cursor)


class ProjectFilesAPI(Resource):
    """Files inside one project."""

    def list(self, project_id: str) -> tuple[list[ProjectFile], list[ProjectFolder]]:
        """Files and the directories containing them.

        Returned as a pair rather than one flattened list: a directory is not a
        file, and collapsing them would make an empty folder indistinguishable
        from a missing one.
        """
        payload = self._get(f"/projects/{project_id}/files", project=project_id)
        if not isinstance(payload, dict):
            return [], []
        return (
            decode_list(ProjectFile, payload.get("items")),
            decode_list(ProjectFolder, payload.get("directories")),
        )

    def upload(
        self,
        project_id: str,
        *,
        filename: str,
        content: bytes | BinaryIO,
        path: str | None = None,
        kind: str = "input",
        media_type: str | None = None,
    ) -> ProjectFile:
        """Upload a file. Multipart, so it does not go through the JSON path."""
        files = {"file": (filename, content, media_type or "application/octet-stream")}
        data: dict[str, str] = {"kind": kind}
        if path is not None:
            data["path"] = path
        response = self._transport.request(
            "POST",
            f"/projects/{project_id}/files",
            files=files,
            data=data,
            project=project_id,
        )
        return decode(ProjectFile, response.data)

    def create_folder(self, project_id: str, path: str) -> ProjectFolder:
        return decode(
            ProjectFolder,
            self._post(f"/projects/{project_id}/folders", json={"path": path}, project=project_id),
        )

    def delete(self, project_id: str, file_id: str) -> bool:
        payload = self._delete(f"/projects/{project_id}/files/{file_id}", project=project_id)
        return isinstance(payload, dict) and payload.get("status") == "deleted"

    def download(self, project_id: str, file_id: str) -> bytes:
        """Raw bytes. Not JSON, so it bypasses the envelope entirely."""
        return self._transport.download(
            f"/projects/{project_id}/files/{file_id}/content", project=project_id
        )


class ProjectSourceAPI(Resource):
    """A project's Git-backed source-control binding (`P6-B`).

    Every mutation is optimistic-concurrency-checked with an ``If-Match``
    etag, mirroring the server's own contract: a stale write 412s rather
    than silently overwriting a change another caller just made. Read the
    current binding with :meth:`get`, pass its ``.source.etag`` back as
    ``if_match``.
    """

    def get(self, project_id: str) -> WorkspaceSourceState:
        return _decode_source_state(self._get(f"/projects/{project_id}/source", project=project_id))

    def configure(
        self,
        project_id: str,
        *,
        provider: str,
        provider_host: str,
        canonical_repository_id: str,
        display_path: str,
        default_branch: str = "main",
        root_path: str = "",
        manifest_path: str = ".caliber/workspace.yaml",
        import_mode: str = "push",
        connection_ref: str | None = None,
        if_match: str | None = None,
    ) -> WorkspaceSourceState:
        """Bind or replace this project's source-of-truth repository.

        ``if_match`` must be omitted the first time a project has no source
        configured yet, and must carry the existing binding's ``etag`` to
        replace one that already exists -- passing one when there is
        nothing to match, or omitting it when there is, both 412.
        Replacing an existing binding also requires it to be disabled
        first (:meth:`disable`).
        """
        body: dict[str, Any] = {
            "provider": provider,
            "provider_host": provider_host,
            "canonical_repository_id": canonical_repository_id,
            "display_path": display_path,
            "default_branch": default_branch,
            "root_path": root_path,
            "manifest_path": manifest_path,
            "import_mode": import_mode,
        }
        if connection_ref is not None:
            body["connection_ref"] = connection_ref
        return _decode_source_state(
            self._put(
                f"/projects/{project_id}/source",
                json=body,
                headers=_if_match_header(if_match),
                project=project_id,
            )
        )

    def enable(self, project_id: str, *, if_match: str) -> WorkspaceSourceState:
        """Verify the binding against its provider and make it importable.

        Each transition method calls its own literal path (rather than
        sharing one helper parameterized on the action) so
        ``docs-site/sdk_coverage.py``'s static source-text scan -- which
        matches a literal ``f"...".`` after ``self._post(``, not a
        runtime-built path -- can see it as covered.
        """
        return _decode_source_state(
            self._post(
                f"/projects/{project_id}/source:enable",
                headers=_if_match_header(if_match),
                project=project_id,
            )
        )

    def disable(self, project_id: str, *, if_match: str) -> WorkspaceSourceState:
        return _decode_source_state(
            self._post(
                f"/projects/{project_id}/source:disable",
                headers=_if_match_header(if_match),
                project=project_id,
            )
        )

    def reconcile(self, project_id: str, *, if_match: str) -> WorkspaceSourceState:
        """Re-verify an already-enabled binding against its provider."""
        return _decode_source_state(
            self._post(
                f"/projects/{project_id}/source:reconcile",
                headers=_if_match_header(if_match),
                project=project_id,
            )
        )

    def capabilities(self, project_id: str) -> WorkspaceSourceCapabilities:
        """What the bound provider supports, without needing credentials."""
        return decode(
            WorkspaceSourceCapabilities,
            self._get(f"/projects/{project_id}/source/capabilities", project=project_id),
        )


class ProjectImportsAPI(Resource):
    """Durable source-to-revision import jobs for one project (`P6-B`)."""

    def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceImportJob]:
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        payload = self._get(
            f"/projects/{project_id}/revision-imports",
            params=params or None,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceImportJob, items, next_cursor)

    def get(self, project_id: str, job_id: str) -> WorkspaceImportJob:
        return decode(
            WorkspaceImportJob,
            self._get(f"/projects/{project_id}/revision-imports/{job_id}", project=project_id),
        )

    def create(
        self,
        project_id: str,
        *,
        repository: str,
        commit_sha: str,
        bundle: bytes | BinaryIO,
        idempotency_key: str,
        filename: str = "bundle",
    ) -> WorkspaceImportJob:
        """Start an import. Multipart, so it does not go through the JSON path.

        ``idempotency_key`` has no default on purpose: the server replays a
        prior job for a reused key rather than starting a second one, so
        generating a fresh key on every call here would silently defeat that
        retry-safety guarantee. Reuse the same key across a retry of the
        *same* logical import; the server compares content digests and
        rejects a key reused for genuinely different content.
        """
        files = {"bundle": (filename, bundle, "application/octet-stream")}
        data = {"repository": repository, "commit_sha": commit_sha}
        response = self._transport.request(
            "POST",
            f"/projects/{project_id}/revision-imports",
            files=files,
            data=data,
            headers={"Idempotency-Key": idempotency_key},
            project=project_id,
        )
        return decode(WorkspaceImportJob, response.data)

    def reconcile(self, project_id: str, job_id: str) -> WorkspaceImportReconciliation:
        """Explicitly observe an import stuck in ``reconcile_required``."""
        return _decode_import_reconciliation(
            self._post(
                f"/projects/{project_id}/revision-imports/{job_id}:reconcile", project=project_id
            )
        )

    def wait(
        self, project_id: str, job_id: str, *, timeout: float = 900.0, **options: Any
    ) -> WorkspaceImportJob:
        """Poll until the import reaches a terminal state.

        ``reconcile_required`` counts as terminal here -- it will never
        advance on its own, so a waiter that only accepted ``succeeded``/
        ``failed`` would block until timeout on the one outcome that needs a
        caller to act (:meth:`reconcile`), not wait longer.
        """
        return wait_for(
            lambda: self.get(project_id, job_id),
            is_done=lambda job: job.is_terminal,
            timeout=timeout,
            **options,
        )


class ProjectRevisionsAPI(Resource):
    """Immutable, reviewable Workspace revisions for one project (`P6-B`)."""

    def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceRevision]:
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        payload = self._get(
            f"/projects/{project_id}/revisions",
            params=params or None,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return CursorPage(
            items=[_decode_revision(item) for item in items] if isinstance(items, list) else [],
            next_cursor=next_cursor,
        )

    def get(self, project_id: str, revision_id: str) -> WorkspaceRevision:
        return _decode_revision(
            self._get(f"/projects/{project_id}/revisions/{revision_id}", project=project_id)
        )

    def diff(self, project_id: str, revision_id: str, *, base: str) -> WorkspaceRevisionDiff:
        """The deterministic pin-level difference from ``base`` to ``revision_id``."""
        return _decode_revision_diff(
            self._get(
                f"/projects/{project_id}/revisions/{revision_id}/diff",
                params={"base": base},
                project=project_id,
            )
        )


class ProjectChangeRequestsAPI(Resource):
    """The Change Request review lifecycle for one project (`P6-B`).

    A Change Request proposes a ready revision for review and promotes it
    through a fixed status machine (``draft`` -> ``open`` -> ... ->
    ``accepted``/``closed``). Mutations that touch a specific version of the
    request (``update_head``/``rebase``/``close``/``assign_reviewer``/
    ``remove_reviewer``) take ``expected_lock_version`` rather than an
    ``If-Match`` header -- that is the server's own optimistic-concurrency
    field here (from a prior ``get()``/``list()`` call's ``.lock_version``),
    unlike :class:`ProjectSourceAPI`'s etag.
    """

    def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        created_by: str | None = None,
        reviewer_user_id: str | None = None,
        semantic_version: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceChangeRequest]:
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if created_by is not None:
            params["created_by"] = created_by
        if reviewer_user_id is not None:
            params["reviewer_user_id"] = reviewer_user_id
        if semantic_version is not None:
            params["semantic_version"] = semantic_version
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        payload = self._get(
            f"/projects/{project_id}/change-requests", params=params or None, project=project_id
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return CursorPage(
            items=[_decode_change_request(item) for item in items]
            if isinstance(items, list)
            else [],
            next_cursor=next_cursor,
        )

    def get(self, project_id: str, change_request_id: str) -> WorkspaceChangeRequest:
        return _decode_change_request(
            self._get(
                f"/projects/{project_id}/change-requests/{change_request_id}", project=project_id
            )
        )

    def create(
        self,
        project_id: str,
        *,
        title: str,
        head_revision_id: str,
        semantic_version: str,
        description: str = "",
        base_revision_id: str | None = None,
        review_backend: str = "caliber",
        reviewer_user_ids: Sequence[str] | None = None,
    ) -> WorkspaceChangeRequest:
        """Open a draft Change Request over a ready revision.

        Stays ``draft`` until :meth:`submit` -- creating one does not by
        itself start review or claim ``semantic_version``.
        """
        body: dict[str, Any] = {
            "title": title,
            "head_revision_id": head_revision_id,
            "semantic_version": semantic_version,
            "description": description,
            "review_backend": review_backend,
            "reviewer_user_ids": list(reviewer_user_ids) if reviewer_user_ids else [],
        }
        if base_revision_id is not None:
            body["base_revision_id"] = base_revision_id
        return _decode_change_request(
            self._post(f"/projects/{project_id}/change-requests", json=body, project=project_id)
        )

    def submit(
        self, project_id: str, change_request_id: str, *, idempotency_key: str | None = None
    ) -> WorkspaceChangeRequest:
        """Move a draft to ``open``, reserving its ``semantic_version`` claim."""
        body = {"idempotency_key": idempotency_key} if idempotency_key is not None else {}
        return _decode_change_request(
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}:submit",
                json=body,
                project=project_id,
            )
        )

    def update_head(
        self,
        project_id: str,
        change_request_id: str,
        *,
        revision_id: str,
        expected_lock_version: int,
        change_summary: str = "",
    ) -> WorkspaceChangeRequest:
        """Append a new ready revision as the request's next head generation."""
        return _decode_change_request(
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}:update-head",
                json={
                    "revision_id": revision_id,
                    "expected_lock_version": expected_lock_version,
                    "change_summary": change_summary,
                },
                project=project_id,
            )
        )

    def rebase(
        self,
        project_id: str,
        change_request_id: str,
        *,
        revision_id: str,
        expected_lock_version: int,
        change_summary: str = "",
        semantic_version: str | None = None,
    ) -> WorkspaceChangeRequest:
        """Bring an ``out_of_date`` request back onto the current accepted head."""
        body: dict[str, Any] = {
            "revision_id": revision_id,
            "expected_lock_version": expected_lock_version,
            "change_summary": change_summary,
        }
        if semantic_version is not None:
            body["semantic_version"] = semantic_version
        return _decode_change_request(
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}:rebase",
                json=body,
                project=project_id,
            )
        )

    def close(
        self, project_id: str, change_request_id: str, *, reason: str, expected_lock_version: int
    ) -> WorkspaceChangeRequest:
        return _decode_change_request(
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}:close",
                json={"reason": reason, "expected_lock_version": expected_lock_version},
                project=project_id,
            )
        )

    def accept(self, project_id: str, change_request_id: str) -> WorkspaceChangeRequest:
        """Accept a Change Request, advancing the project's accepted revision.

        Takes no QA-evidence argument by design: the server derives it
        itself from a durably recorded QA `go` decision bound to the
        request's current head (see
        ``caliber.routes.workspace_change_requests``'s own docstring on the
        server side) rather than trusting anything this client could send.
        A `409` means either no qualifying QA decision exists yet for the
        current head, or a concurrent acceptance already advanced the
        project's accepted revision out from under this request.
        """
        return _decode_change_request(
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}:accept",
                json={},
                project=project_id,
            )
        )

    def list_comments(
        self,
        project_id: str,
        change_request_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceChangeRequestComment]:
        params = _page_params(limit, cursor)
        payload = self._get(
            f"/projects/{project_id}/change-requests/{change_request_id}/comments",
            params=params,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceChangeRequestComment, items, next_cursor)

    def add_comment(
        self,
        project_id: str,
        change_request_id: str,
        *,
        body: str,
        head_id: str | None = None,
        resource_type: str | None = None,
        resource_name: str | None = None,
        source_path: str | None = None,
    ) -> WorkspaceChangeRequestComment:
        """Add a comment, optionally anchored to a specific head/resource/path."""
        payload: dict[str, Any] = {"body": body}
        for key, value in (
            ("head_id", head_id),
            ("resource_type", resource_type),
            ("resource_name", resource_name),
            ("source_path", source_path),
        ):
            if value is not None:
                payload[key] = value
        return decode(
            WorkspaceChangeRequestComment,
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}/comments",
                json=payload,
                project=project_id,
            ),
        )

    def list_reviewers(
        self,
        project_id: str,
        change_request_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceChangeRequestReviewer]:
        params = _page_params(limit, cursor)
        payload = self._get(
            f"/projects/{project_id}/change-requests/{change_request_id}/reviewers",
            params=params,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceChangeRequestReviewer, items, next_cursor)

    def assign_reviewer(
        self, project_id: str, change_request_id: str, user_id: str, *, expected_lock_version: int
    ) -> WorkspaceChangeRequestReviewer:
        return decode(
            WorkspaceChangeRequestReviewer,
            self._put(
                f"/projects/{project_id}/change-requests/{change_request_id}/reviewers/{user_id}",
                json={"expected_lock_version": expected_lock_version},
                project=project_id,
            ),
        )

    def remove_reviewer(
        self, project_id: str, change_request_id: str, user_id: str, *, expected_lock_version: int
    ) -> WorkspaceChangeRequest:
        """Deactivate a reviewer. Returns the Change Request, not the reviewer
        row -- ``active_reviewer_count`` is the reason a caller would check."""
        return _decode_change_request(
            self._delete(
                f"/projects/{project_id}/change-requests/{change_request_id}/reviewers/{user_id}",
                json={"expected_lock_version": expected_lock_version},
                project=project_id,
            )
        )

    def list_reviews(
        self,
        project_id: str,
        change_request_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceChangeRequestReview]:
        params = _page_params(limit, cursor)
        payload = self._get(
            f"/projects/{project_id}/change-requests/{change_request_id}/reviews",
            params=params,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceChangeRequestReview, items, next_cursor)

    def submit_review(
        self,
        project_id: str,
        change_request_id: str,
        *,
        head_id: str,
        decision: str,
        rationale: str = "",
    ) -> WorkspaceChangeRequestReview:
        """Record one reviewer's decision against a specific head.

        ``decision`` is ``"approve"`` or ``"request_changes"``; passed
        through rather than a stricter type so a server that adds a third
        decision is still reachable without an SDK release.
        """
        return decode(
            WorkspaceChangeRequestReview,
            self._post(
                f"/projects/{project_id}/change-requests/{change_request_id}/reviews",
                json={"head_id": head_id, "decision": decision, "rationale": rationale},
                project=project_id,
            ),
        )

    def list_attestations(
        self,
        project_id: str,
        change_request_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceExternalReviewAttestation]:
        params = _page_params(limit, cursor)
        payload = self._get(
            f"/projects/{project_id}/change-requests/{change_request_id}/external-review-attestations",
            params=params,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceExternalReviewAttestation, items, next_cursor)

    def refresh_external_review(self, project_id: str, change_request_id: str) -> None:
        """Queue a re-check of this request's external (e.g. GitHub PR) review state.

        Fire-and-forget: the server responds ``202`` with no resource to
        decode, so there is nothing meaningful to return.
        """
        self._post(
            f"/projects/{project_id}/change-requests/{change_request_id}:refresh-external-review",
            project=project_id,
        )

    def list_checks(
        self,
        project_id: str,
        change_request_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> CursorPage[WorkspaceChangeRequestCheck]:
        params = _page_params(limit, cursor)
        payload = self._get(
            f"/projects/{project_id}/change-requests/{change_request_id}/checks",
            params=params,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceChangeRequestCheck, items, next_cursor)


class ProjectVersionTagsAPI(Resource):
    """Immutable semantic-version claims recorded against revisions (`P6-B`)."""

    def list(
        self, project_id: str, *, limit: int | None = None, cursor: str | None = None
    ) -> CursorPage[WorkspaceVersionTag]:
        params = _page_params(limit, cursor)
        payload = self._get(
            f"/projects/{project_id}/version-tags", params=params, project=project_id
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return _cursor_page(WorkspaceVersionTag, items, next_cursor)

    def get(self, project_id: str, tag: str) -> WorkspaceVersionTag:
        return decode(
            WorkspaceVersionTag,
            self._get(f"/projects/{project_id}/version-tags/{tag}", project=project_id),
        )


class ProjectReleaseOperationsAPI(Resource):
    """Durable apply/rollback intents against one release (`P5-C`).

    Unlike the cursor-paginated Change-Request/import/revision families,
    :meth:`list` uses plain ``limit``/``offset`` -- this resource's own
    immediate server-side sibling (release list/evidence/evaluations, see
    :class:`ProjectReleasesAPI`) already established that convention for
    this release family, and there was no reason to introduce a third
    pagination style where two already coexist in shipped code.
    """

    def list(
        self,
        project_id: str,
        release_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceReleaseOperation]:
        payload = self._get(
            f"/projects/{project_id}/releases/{release_id}/operations",
            params=_offset_params(limit, offset),
            project=project_id,
        )
        return decode_list(WorkspaceReleaseOperation, payload)

    def get(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        return _decode_operation_result(
            self._get(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}",
                project=project_id,
            )
        )

    def create(
        self,
        project_id: str,
        release_id: str,
        *,
        kind: str,
        idempotency_key: str,
        expected_environment_lock_version: int,
        expected_current_release_id: str | None = None,
        target_release_id: str | None = None,
    ) -> WorkspaceReleaseOperationResult:
        """Prepare an ``"apply"`` or ``"rollback"`` operation.

        ``expected_environment_lock_version`` is a compare-and-swap against
        the *environment's* current lock version -- a stale value 409s
        rather than racing another operation targeting the same environment.
        """
        body: dict[str, Any] = {
            "kind": kind,
            "idempotency_key": idempotency_key,
            "expected_environment_lock_version": expected_environment_lock_version,
        }
        if expected_current_release_id is not None:
            body["expected_current_release_id"] = expected_current_release_id
        if target_release_id is not None:
            body["target_release_id"] = target_release_id
        return _decode_operation_result(
            self._post(
                f"/projects/{project_id}/releases/{release_id}/operations",
                json=body,
                project=project_id,
            )
        )

    def apply(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        """Execute a prepared operation through its provider adapter."""
        return _decode_operation_result(
            self._post(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}:apply",
                project=project_id,
            )
        )

    def observe(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        """Re-check an in-flight or ambiguous operation against its provider."""
        return _decode_operation_result(
            self._post(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}:observe",
                project=project_id,
            )
        )

    def cancel_expired(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        """Cancel an operation whose lease has expired without ever applying.

        The literal path stays one f-string passed straight into
        ``self._post(`` (rather than split across adjacent literals, or
        built up in a local variable first) because
        ``docs-site/sdk_coverage.py``'s static source-text scan only matches
        a single quoted literal immediately following ``self._post(`` --
        either alternative would make this exact route silently read as
        uncovered.
        """
        return _decode_operation_result(
            self._post(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}:cancel-expired",
                project=project_id,
            )
        )

    def wait(
        self,
        project_id: str,
        release_id: str,
        operation_id: str,
        *,
        timeout: float = 900.0,
        **options: Any,
    ) -> WorkspaceReleaseOperationResult:
        """Poll until an apply or rollback operation reaches a terminal state.

        ``reconcile_required`` counts as terminal here for the same reason it
        does for :meth:`ProjectImportsAPI.wait` -- it will never advance on
        its own, so a caller who only waited for ``applied``/``failed``
        would block until timeout on the one outcome that needs
        :meth:`observe` called, not more waiting.
        """
        return wait_for(
            lambda: self.get(project_id, release_id, operation_id),
            is_done=lambda result: result.is_terminal,
            timeout=timeout,
            **options,
        )


class ProjectReleasesAPI(Resource):
    """The Workspace release evaluation/decision/approval lifecycle (`P5-F`).

    A release moves through a fixed state machine (``draft`` ->
    ``evaluating`` -> ``{blocked, rejected, approved,
    awaiting_quality_signoff}`` -> ``awaiting_approval`` -> ``{approved,
    rejected}``); :class:`ProjectReleaseOperationsAPI` then executes an
    *approved* release against an environment. Two authorization shapes
    coexist here, mirroring the server routes exactly: :meth:`create`/
    :meth:`evaluate` are plain project-scoped actions, while
    :meth:`quality_signoff`/:meth:`approve`/:meth:`break_glass_apply` are
    identity-specific governance decisions the server authorizes on role
    (Reviewer vs. Owner) and platform scope, not a single scope check.
    """

    def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        environment_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceRelease]:
        payload = self._get(
            f"/projects/{project_id}/releases",
            params=_offset_params(limit, offset, status=status, environment_id=environment_id),
            project=project_id,
        )
        return decode_list(WorkspaceRelease, payload)

    def create(
        self,
        project_id: str,
        *,
        revision_id: str,
        environment_id: str,
        environment_config_sha256: str,
        runtime_dependencies_sha256: str,
        policy_sha256: str,
        request_idempotency_key: str,
        change_request_id: str | None = None,
        change_request_head_id: str | None = None,
        version_tag_id: str | None = None,
        predecessor_release_id: str | None = None,
    ) -> WorkspaceRelease:
        """Capture immutable release coordinates before evaluation dispatch.

        The three ``*_sha256`` digests are pinned here and re-verified at
        every later decision point -- a release evaluated against one
        environment config and then approved against a different one is
        exactly the drift this pinning exists to make impossible.
        """
        body: dict[str, Any] = {
            "revision_id": revision_id,
            "environment_id": environment_id,
            "environment_config_sha256": environment_config_sha256,
            "runtime_dependencies_sha256": runtime_dependencies_sha256,
            "policy_sha256": policy_sha256,
            "request_idempotency_key": request_idempotency_key,
        }
        for key, value in (
            ("change_request_id", change_request_id),
            ("change_request_head_id", change_request_head_id),
            ("version_tag_id", version_tag_id),
            ("predecessor_release_id", predecessor_release_id),
        ):
            if value is not None:
                body[key] = value
        return decode(
            WorkspaceRelease,
            self._post(f"/projects/{project_id}/releases", json=body, project=project_id),
        )

    def get(self, project_id: str, release_id: str) -> WorkspaceRelease:
        return decode(
            WorkspaceRelease,
            self._get(f"/projects/{project_id}/releases/{release_id}", project=project_id),
        )

    def list_evidence(
        self,
        project_id: str,
        release_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceReleaseEvidence]:
        payload = self._get(
            f"/projects/{project_id}/releases/{release_id}/evidence",
            params=_offset_params(limit, offset),
            project=project_id,
        )
        return decode_list(WorkspaceReleaseEvidence, payload)

    def evaluate(
        self,
        project_id: str,
        release_id: str,
        *,
        idempotency_key: str,
        evaluation_plan_sha256: str,
        input_sha256: str,
    ) -> WorkspaceReleaseEvaluation:
        """Request an evaluation attempt; a worker claims and runs it.

        Replayed by ``idempotency_key``: calling this again with the same
        key and digests while an attempt is active returns that same
        attempt rather than starting a second one, but a *different* key
        while one is still active is refused (409) rather than running two
        evaluations concurrently.
        """
        return decode(
            WorkspaceReleaseEvaluation,
            self._post(
                f"/projects/{project_id}/releases/{release_id}/evaluate",
                json={
                    "idempotency_key": idempotency_key,
                    "evaluation_plan_sha256": evaluation_plan_sha256,
                    "input_sha256": input_sha256,
                },
                project=project_id,
            ),
        )

    def list_evaluations(
        self,
        project_id: str,
        release_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceReleaseEvaluation]:
        payload = self._get(
            f"/projects/{project_id}/releases/{release_id}/evaluations",
            params=_offset_params(limit, offset),
            project=project_id,
        )
        return decode_list(WorkspaceReleaseEvaluation, payload)

    def get_evaluation(
        self, project_id: str, release_id: str, evaluation_id: str
    ) -> WorkspaceReleaseEvaluation:
        return decode(
            WorkspaceReleaseEvaluation,
            self._get(
                f"/projects/{project_id}/releases/{release_id}/evaluations/{evaluation_id}",
                project=project_id,
            ),
        )

    def wait_for_evaluation(
        self,
        project_id: str,
        release_id: str,
        evaluation_id: str,
        *,
        timeout: float = 900.0,
        **options: Any,
    ) -> WorkspaceReleaseEvaluation:
        """Poll until the evaluation attempt reaches ``succeeded`` or ``failed``."""
        return wait_for(
            lambda: self.get_evaluation(project_id, release_id, evaluation_id),
            is_done=lambda evaluation: evaluation.is_terminal,
            timeout=timeout,
            **options,
        )

    def quality_signoff(
        self,
        project_id: str,
        release_id: str,
        *,
        decision: str,
        gate_evidence_sha256: str,
        rationale: str = "",
        change_request_head_id: str | None = None,
    ) -> WorkspaceReleaseDecision:
        """Record a QA go/no-go decision. Requires the Reviewer project role.

        ``decision`` is ``"go"`` or ``"no_go"``, passed through rather than a
        stricter type for the same forward-compatibility reason as
        :meth:`ProjectChangeRequestsAPI.submit_review`'s ``decision``.
        """
        return decode(
            WorkspaceReleaseDecision,
            self._post(
                f"/projects/{project_id}/releases/{release_id}/quality-signoff",
                json=_decision_body(
                    decision, gate_evidence_sha256, rationale, change_request_head_id
                ),
                project=project_id,
            ),
        )

    def approve(
        self,
        project_id: str,
        release_id: str,
        *,
        decision: str,
        gate_evidence_sha256: str,
        rationale: str = "",
        change_request_head_id: str | None = None,
    ) -> WorkspaceReleaseDecision:
        """Record the final release go/no-go decision. Requires the Owner role.

        Refused (409) unless the release already carries a *fresh* quality
        signoff bound to this exact head/revision/digests -- an approval
        does not itself re-run or supersede quality review.
        """
        return decode(
            WorkspaceReleaseDecision,
            self._post(
                f"/projects/{project_id}/releases/{release_id}/approve",
                json=_decision_body(
                    decision, gate_evidence_sha256, rationale, change_request_head_id
                ),
                project=project_id,
            ),
        )

    def break_glass_apply(
        self,
        project_id: str,
        release_id: str,
        *,
        reason: str,
        incident_ref: str,
        authorization_ref: str,
        expires_at: str,
        gate_evidence_sha256: str,
        expected_current_release_id: str,
        expected_environment_lock_version: int,
        idempotency_key: str,
    ) -> WorkspaceBreakGlassApplyResult:
        """Interactive production recovery, bypassing the normal apply path.

        Requires a real browser session (platform Admin, ``credential_kind
        == "session"``) -- a personal access token can never call this,
        by server-side design, not merely by convention. ``expires_at`` is
        an ISO-8601 timestamp string; the authorization is void past it
        regardless of whether it was ever used. ``expected_current_release_id``
        is a compare-and-swap against *this* release (not the environment's
        currently-deployed one) -- it fails closed if the release moved on
        while the authorization was being requested.
        """
        return decode(
            WorkspaceBreakGlassApplyResult,
            self._post(
                f"/projects/{project_id}/releases/{release_id}/break-glass-apply",
                json={
                    "reason": reason,
                    "incident_ref": incident_ref,
                    "authorization_ref": authorization_ref,
                    "expires_at": expires_at,
                    "gate_evidence_sha256": gate_evidence_sha256,
                    "expected_current_release_id": expected_current_release_id,
                    "expected_environment_lock_version": expected_environment_lock_version,
                    "idempotency_key": idempotency_key,
                },
                project=project_id,
            ),
        )


class ProjectReworkTasksAPI(Resource):
    """Rework tasks owned by one project-scoped agent population."""

    def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
    ) -> list[ReworkTask]:
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if assigned_to is not None:
            params["assigned_to"] = assigned_to
        return decode_list(
            ReworkTask,
            self._get(
                f"/projects/{project_id}/rework-tasks",
                params=params or None,
                project=project_id,
            ),
        )

    def get(self, project_id: str, task_id: str) -> ReworkTask:
        return decode(
            ReworkTask,
            self._get(f"/projects/{project_id}/rework-tasks/{task_id}", project=project_id),
        )

    def claim(self, project_id: str, task_id: str) -> ReworkTask:
        return decode(
            ReworkTask,
            self._post(f"/projects/{project_id}/rework-tasks/{task_id}:claim", project=project_id),
        )

    def resolve(self, project_id: str, task_id: str, **options: Any) -> ReworkTask:
        return decode(
            ReworkTask,
            self._post(
                f"/projects/{project_id}/rework-tasks/{task_id}:resolve",
                json=options,
                project=project_id,
            ),
        )

    def reassign(self, project_id: str, task_id: str, assigned_to: str) -> ReworkTask:
        return decode(
            ReworkTask,
            self._post(
                f"/projects/{project_id}/rework-tasks/{task_id}:reassign",
                json={"assigned_to": assigned_to},
                project=project_id,
            ),
        )


class ProjectMembersAPI(Resource):
    """A project's membership, role, and primary-ownership management (`P6-B`).

    Exposed as ``ProjectsAPI.members``. The equivalent flat methods on
    ``ProjectsAPI`` itself (``list_members``/``add_member``/
    ``update_member``/``remove_member``/``transfer_ownership``) delegate to
    this class -- a root convenience for the common case, not a second
    implementation to keep in sync.
    """

    def list(self, project_id: str) -> _List[ProjectMember]:
        """List active members and their effective project roles."""
        payload = self._get(f"/projects/{project_id}/members", project=project_id)
        if not isinstance(payload, dict):
            return []
        return decode_list(ProjectMember, payload.get("members"))

    def add(self, project_id: str, user_id: str, *, role: str = "viewer") -> ProjectMember:
        """Grant ``user_id`` a project role; only owners may manage members."""
        return decode(
            ProjectMember,
            self._post(
                f"/projects/{project_id}/members",
                json={"user_id": user_id, "role": role},
                project=project_id,
            ),
        )

    def update(
        self,
        project_id: str,
        user_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> ProjectMember:
        """Change a member's role or active status."""
        body: dict[str, Any] = {}
        if role is not None:
            body["role"] = role
        if status is not None:
            body["status"] = status
        return decode(
            ProjectMember,
            self._patch(f"/projects/{project_id}/members/{user_id}", json=body, project=project_id),
        )

    def remove(self, project_id: str, user_id: str) -> bool:
        """Deactivate a member; the project owner cannot be removed."""
        payload = self._delete(f"/projects/{project_id}/members/{user_id}", project=project_id)
        return isinstance(payload, dict) and payload.get("removed") is True

    def transfer_ownership(self, project_id: str, new_owner_user_id: str) -> Project:
        """Atomically move the primary-owner pointer to another active,
        eligible ``owner``-role (Admin) member.

        Only the current primary owner may call this; the target must
        already hold the ``owner`` role (see :meth:`add`/:meth:`update`) and
        pass a live scope-eligibility check.
        """
        return decode(
            Project,
            self._post(
                f"/projects/{project_id}/transfer-ownership",
                json={"new_owner_user_id": new_owner_user_id},
                project=project_id,
            ),
        )


class ProjectsAPI(Resource):
    """Projects, project access, and the file sub-resource."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.files = ProjectFilesAPI(transport)
        self.members = ProjectMembersAPI(transport)
        self.rework_tasks = ProjectReworkTasksAPI(transport)
        self.source = ProjectSourceAPI(transport)
        self.imports = ProjectImportsAPI(transport)
        self.revisions = ProjectRevisionsAPI(transport)
        self.change_requests = ProjectChangeRequestsAPI(transport)
        self.version_tags = ProjectVersionTagsAPI(transport)
        self.releases = ProjectReleasesAPI(transport)
        self.release_operations = ProjectReleaseOperationsAPI(transport)

    def list(self, *, status: str | None = None) -> list[Project]:
        """Active projects by default; pass ``status="all"`` for everything."""
        params = {"status": status} if status else None
        return decode_list(Project, self._get("/projects", params=params))

    def get(self, project_id: str) -> Project:
        return decode(Project, self._get(f"/projects/{project_id}", project=project_id))

    def create(self, name: str, *, description: str | None = None) -> Project:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        return decode(Project, self._post("/projects", json=body))

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Project:
        """Rename/redescribe a project, and (deprecated) flip its lifecycle status.

        Section 13.3's compatibility contract: ``status`` stays accepted
        during a deprecation window rather than becoming a hard `TypeError`
        (the server itself now rejects a ``status`` field on the underlying
        ``PATCH`` route with a ``400`` -- name/description only). Passing it
        here still works, emits a ``DeprecationWarning``, and delegates to
        ``archive()``/``restore()`` -- the new Admin-only lifecycle routes,
        which also record who made the change and when (``archived_at``/
        ``archived_by`` on the returned ``Project``). Passing both ``status``
        and ``name``/``description`` together sends two requests and returns
        the second (name/description) response, which reflects both.

        Prefer calling ``archive()``/``restore()`` directly in new code.
        """
        result: Project | None = None
        if status is not None:
            if status not in {"active", "archived"}:
                raise ValueError(f"unsupported status {status!r}; expected 'active' or 'archived'")
            warnings.warn(
                "ProjectsAPI.update(status=...) is deprecated; call archive()/restore() "
                "directly. The server now enforces this as an Admin-only lifecycle "
                "transition, separate from name/description updates.",
                DeprecationWarning,
                stacklevel=2,
            )
            result = self.archive(project_id) if status == "archived" else self.restore(project_id)
        body: dict[str, Any] = {}
        for key, value in (("name", name), ("description", description)):
            if value is not None:
                body[key] = value
        if body or result is None:
            result = decode(
                Project, self._patch(f"/projects/{project_id}", json=body, project=project_id)
            )
        return result

    def archive(self, project_id: str) -> Project:
        """Move a project to the ``archived`` status, recording who/when."""
        return decode(Project, self._post(f"/projects/{project_id}/archive", project=project_id))

    def restore(self, project_id: str) -> Project:
        """Move an archived project back to ``active``, clearing provenance."""
        return decode(Project, self._post(f"/projects/{project_id}/restore", project=project_id))

    def transfer_ownership(self, project_id: str, new_owner_user_id: str) -> Project:
        """Delegates to :meth:`ProjectMembersAPI.transfer_ownership` (``self.members``)."""
        return self.members.transfer_ownership(project_id, new_owner_user_id)

    def list_members(self, project_id: str) -> _List[ProjectMember]:
        """Delegates to :meth:`ProjectMembersAPI.list` (``self.members``)."""
        return self.members.list(project_id)

    def add_member(self, project_id: str, user_id: str, *, role: str = "viewer") -> ProjectMember:
        """Delegates to :meth:`ProjectMembersAPI.add` (``self.members``)."""
        return self.members.add(project_id, user_id, role=role)

    def update_member(
        self,
        project_id: str,
        user_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> ProjectMember:
        """Delegates to :meth:`ProjectMembersAPI.update` (``self.members``)."""
        return self.members.update(project_id, user_id, role=role, status=status)

    def remove_member(self, project_id: str, user_id: str) -> bool:
        """Delegates to :meth:`ProjectMembersAPI.remove` (``self.members``)."""
        return self.members.remove(project_id, user_id)

    def storage(self) -> Any:
        """Where project files live, and what else the deployment supports."""
        return self._get("/projects/storage")

    def list_environments(self, project_id: str) -> _List[WorkspaceEnvironment]:
        """The project's four fixed environments, in promotion order."""
        payload = self._get(f"/projects/{project_id}/environments", project=project_id)
        if not isinstance(payload, dict):
            return []
        return decode_list(WorkspaceEnvironment, payload.get("environments"))

    def get_environment(self, project_id: str, name: str) -> WorkspaceEnvironment:
        return decode(
            WorkspaceEnvironment,
            self._get(f"/projects/{project_id}/environments/{name}", project=project_id),
        )

    def update_environment(
        self,
        project_id: str,
        name: str,
        *,
        policy: dict[str, Any],
        policy_sha256: str,
        expected_lock_version: int,
    ) -> WorkspaceEnvironment:
        """Update an environment's policy configuration.

        ``policy_sha256`` is a caller-supplied digest, trusted the same way
        the release-lifecycle digests are (``environment_config_sha256``,
        ``runtime_dependencies_sha256``, ...) -- the server stores it and
        ``policy`` as given, it does not independently recompute or verify
        the hash. ``expected_lock_version`` is a compare-and-swap against
        this environment's own ``lock_version`` (from a prior
        :meth:`get_environment`/:meth:`list_environments` call); a stale
        value 409s rather than silently overwriting a change another caller
        just made. Identity fields (``name``/``environment_class``/
        ``promotion_order``) can never be changed here or anywhere else.
        """
        return decode(
            WorkspaceEnvironment,
            self._patch(
                f"/projects/{project_id}/environments/{name}",
                json={
                    "policy": policy,
                    "policy_sha256": policy_sha256,
                    "expected_lock_version": expected_lock_version,
                },
                project=project_id,
            ),
        )

    def enable_environment(self, project_id: str, name: str) -> WorkspaceEnvironment:
        """Explicit lifecycle transition to ``"active"``; Admin-only."""
        return decode(
            WorkspaceEnvironment,
            self._post(f"/projects/{project_id}/environments/{name}/enable", project=project_id),
        )

    def disable_environment(self, project_id: str, name: str) -> WorkspaceEnvironment:
        """Explicit lifecycle transition to ``"disabled"``; Admin-only."""
        return decode(
            WorkspaceEnvironment,
            self._post(f"/projects/{project_id}/environments/{name}/disable", project=project_id),
        )


# Workspace is the public product term; the wire API and the legacy class name
# remain ProjectsAPI for compatibility.
WorkspacesAPI = ProjectsAPI

__all__ = [
    "ProjectChangeRequestsAPI",
    "ProjectFilesAPI",
    "ProjectImportsAPI",
    "ProjectMembersAPI",
    "ProjectReleaseOperationsAPI",
    "ProjectReleasesAPI",
    "ProjectRevisionsAPI",
    "ProjectReworkTasksAPI",
    "ProjectSourceAPI",
    "ProjectVersionTagsAPI",
    "ProjectsAPI",
    "WorkspacesAPI",
]
