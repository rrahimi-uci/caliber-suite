"""Asynchronous project, workspace, and project-file operations.

The route and model surface intentionally mirrors
``caliber_sdk.resources.projects``. The transport remains the only thing that
changes: every operation awaits the same request, envelope, retry, and project
scope machinery used by the rest of the async client.

Async parity here is deliberately partial, following the same criteria
:mod:`caliber_sdk.aio.client`'s own docstring states for the rest of this
package: root/member/storage/file/environment operations are covered because
they were already async before this module grew further (`P6-A`/`P6-B`), and
:class:`AsyncProjectImportsAPI`/:class:`AsyncProjectReleasesAPI`/
:class:`AsyncProjectReleaseOperationsAPI` (`P6-C`) are covered because each
has a ``wait()``-shaped long-running-poll operation -- exactly the case that
module names as where being async changes the outcome. The remaining `P6-B`
grouped resources (``ProjectSourceAPI``, ``ProjectRevisionsAPI``,
``ProjectChangeRequestsAPI``, ``ProjectVersionTagsAPI``, ``ProjectMembersAPI``
as a grouped form, ``ProjectReworkTasksAPI``) have no polling, streaming, or
concurrency-shaped operation and stay sync-only by the same reasoning that
already excludes ~40 other resource modules -- use the synchronous client for
those, or ``client.raw`` plus ``caliber_sdk.models.decode``.
"""

from __future__ import annotations

import warnings
from typing import Any, BinaryIO

from ..models._decode import decode, decode_list
from ..models.common import CursorPage
from ..models.core import Project, ProjectFile, ProjectFolder, ProjectMember, WorkspaceEnvironment
from ..models.workspace import (
    WorkspaceBreakGlassApplyResult,
    WorkspaceImportJob,
    WorkspaceImportReconciliation,
    WorkspaceRelease,
    WorkspaceReleaseDecision,
    WorkspaceReleaseEvaluation,
    WorkspaceReleaseEvidence,
    WorkspaceReleaseOperation,
    WorkspaceReleaseOperationItem,
    WorkspaceReleaseOperationResult,
)
from ._base import _AsyncResource
from .transport import AsyncTransport
from .waiters import wait_for

_List = list


def _list_items_and_cursor(payload: Any) -> tuple[Any, str | None]:
    """See :func:`caliber_sdk.resources.projects._list_items_and_cursor` --
    tolerates both wire shapes this SDK's cursor-paginated endpoints use."""
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else data
    next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
    return items, next_cursor if isinstance(next_cursor, str) else None


def _decode_import_reconciliation(payload: Any) -> WorkspaceImportReconciliation:
    """See :func:`caliber_sdk.resources.projects._decode_import_reconciliation`."""
    result = decode(WorkspaceImportReconciliation, payload)
    job_payload = payload.get("job") if isinstance(payload, dict) else None
    result.job = decode(WorkspaceImportJob, job_payload)
    return result


def _decode_operation_result(payload: Any) -> WorkspaceReleaseOperationResult:
    """See :func:`caliber_sdk.resources.projects._decode_operation_result`."""
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


class AsyncProjectFilesAPI(_AsyncResource):
    """Files inside one project."""

    async def list(self, project_id: str) -> tuple[list[ProjectFile], list[ProjectFolder]]:
        """Return files and directories separately."""
        payload = await self._get(f"/projects/{project_id}/files", project=project_id)
        if not isinstance(payload, dict):
            return [], []
        return (
            decode_list(ProjectFile, payload.get("items")),
            decode_list(ProjectFolder, payload.get("directories")),
        )

    async def upload(
        self,
        project_id: str,
        *,
        filename: str,
        content: bytes | BinaryIO,
        path: str | None = None,
        kind: str = "input",
        media_type: str | None = None,
    ) -> ProjectFile:
        """Upload a project file using the async transport's multipart path."""
        files = {"file": (filename, content, media_type or "application/octet-stream")}
        data: dict[str, str] = {"kind": kind}
        if path is not None:
            data["path"] = path
        response = await self._transport.request(
            "POST",
            f"/projects/{project_id}/files",
            files=files,
            data=data,
            project=project_id,
        )
        return decode(ProjectFile, response.data)

    async def create_folder(self, project_id: str, path: str) -> ProjectFolder:
        return decode(
            ProjectFolder,
            await self._post(
                f"/projects/{project_id}/folders", json={"path": path}, project=project_id
            ),
        )

    async def delete(self, project_id: str, file_id: str) -> bool:
        payload = await self._delete(f"/projects/{project_id}/files/{file_id}", project=project_id)
        return isinstance(payload, dict) and payload.get("status") == "deleted"

    async def download(self, project_id: str, file_id: str) -> bytes:
        """Download raw file bytes without JSON envelope handling."""
        return await self._transport.download(
            f"/projects/{project_id}/files/{file_id}/content", project=project_id
        )


class AsyncProjectImportsAPI(_AsyncResource):
    """Durable source-to-revision import jobs for one project (`P6-C`), awaited.

    Async parity for this resource -- unlike most of `P6-B`'s other grouped
    resources -- earns its complexity under :mod:`caliber_sdk.aio.client`'s
    own stated criteria: :meth:`wait` is exactly the "long-running work you
    poll" category that module names as where async changes the outcome.
    """

    async def list(
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
        payload = await self._get(
            f"/projects/{project_id}/revision-imports",
            params=params or None,
            project=project_id,
        )
        items, next_cursor = _list_items_and_cursor(payload)
        return CursorPage(items=decode_list(WorkspaceImportJob, items), next_cursor=next_cursor)

    async def get(self, project_id: str, job_id: str) -> WorkspaceImportJob:
        return decode(
            WorkspaceImportJob,
            await self._get(
                f"/projects/{project_id}/revision-imports/{job_id}", project=project_id
            ),
        )

    async def create(
        self,
        project_id: str,
        *,
        repository: str,
        commit_sha: str,
        bundle: bytes | BinaryIO,
        idempotency_key: str,
        filename: str = "bundle",
    ) -> WorkspaceImportJob:
        """Start an import. See the sync ``create``'s docstring for why
        ``idempotency_key`` has no default."""
        files = {"bundle": (filename, bundle, "application/octet-stream")}
        data = {"repository": repository, "commit_sha": commit_sha}
        response = await self._transport.request(
            "POST",
            f"/projects/{project_id}/revision-imports",
            files=files,
            data=data,
            headers={"Idempotency-Key": idempotency_key},
            project=project_id,
        )
        return decode(WorkspaceImportJob, response.data)

    async def reconcile(self, project_id: str, job_id: str) -> WorkspaceImportReconciliation:
        """Explicitly observe an import stuck in ``reconcile_required``."""
        return _decode_import_reconciliation(
            await self._post(
                f"/projects/{project_id}/revision-imports/{job_id}:reconcile", project=project_id
            )
        )

    async def wait(
        self, project_id: str, job_id: str, *, timeout: float = 900.0, **options: Any
    ) -> WorkspaceImportJob:
        """Poll until the import reaches a terminal state; see the sync
        ``wait``'s docstring for why ``reconcile_required`` counts as one."""
        return await wait_for(
            lambda: self.get(project_id, job_id),
            is_done=lambda job: job.is_terminal,
            timeout=timeout,
            **options,
        )


class AsyncProjectReleasesAPI(_AsyncResource):
    """The Workspace release evaluation/decision/approval lifecycle (`P5-F`),
    awaited.

    Async parity here earns its complexity two ways
    :mod:`caliber_sdk.aio.client` names: :meth:`wait_for_evaluation` is
    long-running work you poll, and awaiting many projects' releases
    concurrently is the same "forty workflow runs" case that module cites
    for :class:`AsyncWorkflowRunsAPI`.
    """

    async def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        environment_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceRelease]:
        payload = await self._get(
            f"/projects/{project_id}/releases",
            params=_offset_params(limit, offset, status=status, environment_id=environment_id),
            project=project_id,
        )
        return decode_list(WorkspaceRelease, payload)

    async def create(
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
            await self._post(f"/projects/{project_id}/releases", json=body, project=project_id),
        )

    async def get(self, project_id: str, release_id: str) -> WorkspaceRelease:
        return decode(
            WorkspaceRelease,
            await self._get(f"/projects/{project_id}/releases/{release_id}", project=project_id),
        )

    async def list_evidence(
        self,
        project_id: str,
        release_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceReleaseEvidence]:
        payload = await self._get(
            f"/projects/{project_id}/releases/{release_id}/evidence",
            params=_offset_params(limit, offset),
            project=project_id,
        )
        return decode_list(WorkspaceReleaseEvidence, payload)

    async def evaluate(
        self,
        project_id: str,
        release_id: str,
        *,
        idempotency_key: str,
        evaluation_plan_sha256: str,
        input_sha256: str,
    ) -> WorkspaceReleaseEvaluation:
        return decode(
            WorkspaceReleaseEvaluation,
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/evaluate",
                json={
                    "idempotency_key": idempotency_key,
                    "evaluation_plan_sha256": evaluation_plan_sha256,
                    "input_sha256": input_sha256,
                },
                project=project_id,
            ),
        )

    async def list_evaluations(
        self,
        project_id: str,
        release_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceReleaseEvaluation]:
        payload = await self._get(
            f"/projects/{project_id}/releases/{release_id}/evaluations",
            params=_offset_params(limit, offset),
            project=project_id,
        )
        return decode_list(WorkspaceReleaseEvaluation, payload)

    async def get_evaluation(
        self, project_id: str, release_id: str, evaluation_id: str
    ) -> WorkspaceReleaseEvaluation:
        return decode(
            WorkspaceReleaseEvaluation,
            await self._get(
                f"/projects/{project_id}/releases/{release_id}/evaluations/{evaluation_id}",
                project=project_id,
            ),
        )

    async def wait_for_evaluation(
        self,
        project_id: str,
        release_id: str,
        evaluation_id: str,
        *,
        timeout: float = 900.0,
        **options: Any,
    ) -> WorkspaceReleaseEvaluation:
        """Poll until the evaluation attempt reaches ``succeeded`` or ``failed``."""
        return await wait_for(
            lambda: self.get_evaluation(project_id, release_id, evaluation_id),
            is_done=lambda evaluation: evaluation.is_terminal,
            timeout=timeout,
            **options,
        )

    async def quality_signoff(
        self,
        project_id: str,
        release_id: str,
        *,
        decision: str,
        gate_evidence_sha256: str,
        rationale: str = "",
        change_request_head_id: str | None = None,
    ) -> WorkspaceReleaseDecision:
        return decode(
            WorkspaceReleaseDecision,
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/quality-signoff",
                json=_decision_body(
                    decision, gate_evidence_sha256, rationale, change_request_head_id
                ),
                project=project_id,
            ),
        )

    async def approve(
        self,
        project_id: str,
        release_id: str,
        *,
        decision: str,
        gate_evidence_sha256: str,
        rationale: str = "",
        change_request_head_id: str | None = None,
    ) -> WorkspaceReleaseDecision:
        return decode(
            WorkspaceReleaseDecision,
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/approve",
                json=_decision_body(
                    decision, gate_evidence_sha256, rationale, change_request_head_id
                ),
                project=project_id,
            ),
        )

    async def break_glass_apply(
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
        return decode(
            WorkspaceBreakGlassApplyResult,
            await self._post(
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


class AsyncProjectReleaseOperationsAPI(_AsyncResource):
    """Durable apply/rollback intents against one release (`P5-C`), awaited.

    Async parity here earns its complexity the same way
    :class:`AsyncProjectReleasesAPI` does: :meth:`wait` is long-running work
    you poll.
    """

    async def list(
        self,
        project_id: str,
        release_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[WorkspaceReleaseOperation]:
        payload = await self._get(
            f"/projects/{project_id}/releases/{release_id}/operations",
            params=_offset_params(limit, offset),
            project=project_id,
        )
        return decode_list(WorkspaceReleaseOperation, payload)

    async def get(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        return _decode_operation_result(
            await self._get(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}",
                project=project_id,
            )
        )

    async def create(
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
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/operations",
                json=body,
                project=project_id,
            )
        )

    async def apply(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        return _decode_operation_result(
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}:apply",
                project=project_id,
            )
        )

    async def observe(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        return _decode_operation_result(
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}:observe",
                project=project_id,
            )
        )

    async def cancel_expired(
        self, project_id: str, release_id: str, operation_id: str
    ) -> WorkspaceReleaseOperationResult:
        """See the sync ``cancel_expired``'s docstring for why the literal
        path stays one f-string passed straight into ``self._post(``."""
        return _decode_operation_result(
            await self._post(
                f"/projects/{project_id}/releases/{release_id}/operations/{operation_id}:cancel-expired",
                project=project_id,
            )
        )

    async def wait(
        self,
        project_id: str,
        release_id: str,
        operation_id: str,
        *,
        timeout: float = 900.0,
        **options: Any,
    ) -> WorkspaceReleaseOperationResult:
        """Poll until an apply or rollback operation reaches a terminal state;
        see the sync ``wait``'s docstring for why ``reconcile_required``
        counts as one."""
        return await wait_for(
            lambda: self.get(project_id, release_id, operation_id),
            is_done=lambda result: result.is_terminal,
            timeout=timeout,
            **options,
        )


class AsyncProjectsAPI(_AsyncResource):
    """Projects, project access, environments, and their file sub-resource."""

    def __init__(self, transport: AsyncTransport) -> None:
        super().__init__(transport)
        self.files = AsyncProjectFilesAPI(transport)
        self.imports = AsyncProjectImportsAPI(transport)
        self.releases = AsyncProjectReleasesAPI(transport)
        self.release_operations = AsyncProjectReleaseOperationsAPI(transport)

    async def list(self, *, status: str | None = None) -> list[Project]:
        """Active projects by default; pass ``status=\"all\"`` for everything."""
        params = {"status": status} if status else None
        return decode_list(Project, await self._get("/projects", params=params))

    async def get(self, project_id: str) -> Project:
        return decode(Project, await self._get(f"/projects/{project_id}", project=project_id))

    async def create(self, name: str, *, description: str | None = None) -> Project:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        return decode(Project, await self._post("/projects", json=body))

    async def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Project:
        """Update metadata, retaining sync compatibility for ``status``."""
        result: Project | None = None
        if status is not None:
            if status not in {"active", "archived"}:
                raise ValueError(f"unsupported status {status!r}; expected 'active' or 'archived'")
            warnings.warn(
                "AsyncProjectsAPI.update(status=...) is deprecated; call archive()/restore() "
                "directly. The server now enforces this as an Admin-only lifecycle "
                "transition, separate from name/description updates.",
                DeprecationWarning,
                stacklevel=2,
            )
            if status == "archived":
                result = await self.archive(project_id)
            else:
                result = await self.restore(project_id)
        body: dict[str, Any] = {}
        for key, value in (("name", name), ("description", description)):
            if value is not None:
                body[key] = value
        if body or result is None:
            result = decode(
                Project,
                await self._patch(f"/projects/{project_id}", json=body, project=project_id),
            )
        return result

    async def archive(self, project_id: str) -> Project:
        """Move a project to ``archived`` and record transition provenance."""
        return decode(
            Project, await self._post(f"/projects/{project_id}/archive", project=project_id)
        )

    async def restore(self, project_id: str) -> Project:
        """Move an archived project back to ``active``."""
        return decode(
            Project, await self._post(f"/projects/{project_id}/restore", project=project_id)
        )

    async def transfer_ownership(self, project_id: str, new_owner_user_id: str) -> Project:
        return decode(
            Project,
            await self._post(
                f"/projects/{project_id}/transfer-ownership",
                json={"new_owner_user_id": new_owner_user_id},
                project=project_id,
            ),
        )

    async def list_members(self, project_id: str) -> _List[ProjectMember]:
        payload = await self._get(f"/projects/{project_id}/members", project=project_id)
        if not isinstance(payload, dict):
            return []
        return decode_list(ProjectMember, payload.get("members"))

    async def add_member(
        self, project_id: str, user_id: str, *, role: str = "viewer"
    ) -> ProjectMember:
        return decode(
            ProjectMember,
            await self._post(
                f"/projects/{project_id}/members",
                json={"user_id": user_id, "role": role},
                project=project_id,
            ),
        )

    async def update_member(
        self,
        project_id: str,
        user_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> ProjectMember:
        body: dict[str, Any] = {}
        if role is not None:
            body["role"] = role
        if status is not None:
            body["status"] = status
        return decode(
            ProjectMember,
            await self._patch(
                f"/projects/{project_id}/members/{user_id}",
                json=body,
                project=project_id,
            ),
        )

    async def remove_member(self, project_id: str, user_id: str) -> bool:
        payload = await self._delete(
            f"/projects/{project_id}/members/{user_id}", project=project_id
        )
        return isinstance(payload, dict) and payload.get("removed") is True

    async def storage(self) -> Any:
        """Return deployment storage capabilities."""
        return await self._get("/projects/storage")

    async def list_environments(self, project_id: str) -> _List[WorkspaceEnvironment]:
        payload = await self._get(f"/projects/{project_id}/environments", project=project_id)
        if not isinstance(payload, dict):
            return []
        return decode_list(WorkspaceEnvironment, payload.get("environments"))

    async def get_environment(self, project_id: str, name: str) -> WorkspaceEnvironment:
        return decode(
            WorkspaceEnvironment,
            await self._get(f"/projects/{project_id}/environments/{name}", project=project_id),
        )

    async def update_environment(
        self,
        project_id: str,
        name: str,
        *,
        policy: dict[str, Any],
        policy_sha256: str,
        expected_lock_version: int,
    ) -> WorkspaceEnvironment:
        """Update an environment's policy configuration.

        See :meth:`caliber_sdk.resources.projects.ProjectsAPI.update_environment`
        for the CAS/digest semantics -- identical here, just awaited.
        """
        return decode(
            WorkspaceEnvironment,
            await self._patch(
                f"/projects/{project_id}/environments/{name}",
                json={
                    "policy": policy,
                    "policy_sha256": policy_sha256,
                    "expected_lock_version": expected_lock_version,
                },
                project=project_id,
            ),
        )

    async def enable_environment(self, project_id: str, name: str) -> WorkspaceEnvironment:
        return decode(
            WorkspaceEnvironment,
            await self._post(
                f"/projects/{project_id}/environments/{name}/enable", project=project_id
            ),
        )

    async def disable_environment(self, project_id: str, name: str) -> WorkspaceEnvironment:
        return decode(
            WorkspaceEnvironment,
            await self._post(
                f"/projects/{project_id}/environments/{name}/disable", project=project_id
            ),
        )


AsyncWorkspacesAPI = AsyncProjectsAPI

__all__ = [
    "AsyncProjectFilesAPI",
    "AsyncProjectImportsAPI",
    "AsyncProjectReleaseOperationsAPI",
    "AsyncProjectReleasesAPI",
    "AsyncProjectsAPI",
    "AsyncWorkspacesAPI",
]
