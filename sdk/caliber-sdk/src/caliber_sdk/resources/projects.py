"""Projects and their files — the workspace-scoping surface.

Every other resource can be scoped to a project via the ``X-CALIBER-Project``
header, which the client sets for you. This module manages the projects
themselves, the files they hold, and (`P6-B`) the source-to-revision import
and revision-review lifecycle: :class:`ProjectImportsAPI` and
:class:`ProjectRevisionsAPI`, exposed as ``ProjectsAPI.imports``/``.revisions``.
"""

from __future__ import annotations

import warnings
from typing import Any, BinaryIO

from ..models._decode import decode, decode_list
from ..models.common import CursorPage
from ..models.core import Project, ProjectFile, ProjectFolder, ProjectMember, WorkspaceEnvironment
from ..models.operations import ReworkTask
from ..models.workspace import (
    WorkspaceImportJob,
    WorkspaceImportReconciliation,
    WorkspaceRevision,
    WorkspaceRevisionDiff,
    WorkspaceRevisionResource,
)
from ..waiters import wait_for
from ._base import Resource

_List = list


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
        data = payload.get("data") if isinstance(payload, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
        return CursorPage(
            items=decode_list(WorkspaceImportJob, items),
            next_cursor=next_cursor if isinstance(next_cursor, str) else None,
        )

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
        data = payload.get("data") if isinstance(payload, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
        return CursorPage(
            items=[_decode_revision(item) for item in items] if isinstance(items, list) else [],
            next_cursor=next_cursor if isinstance(next_cursor, str) else None,
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


class ProjectsAPI(Resource):
    """Projects, project access, and the file sub-resource."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.files = ProjectFilesAPI(transport)
        self.rework_tasks = ProjectReworkTasksAPI(transport)
        self.imports = ProjectImportsAPI(transport)
        self.revisions = ProjectRevisionsAPI(transport)

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
        """Atomically move the primary-owner pointer to another active,
        eligible ``owner``-role (Admin) member.

        Only the current primary owner may call this; the target must
        already hold the ``owner`` role (see ``add_member``/
        ``update_member``) and pass a live scope-eligibility check.
        """
        return decode(
            Project,
            self._post(
                f"/projects/{project_id}/transfer-ownership",
                json={"new_owner_user_id": new_owner_user_id},
                project=project_id,
            ),
        )

    def list_members(self, project_id: str) -> _List[ProjectMember]:
        """List active members and their effective project roles."""
        payload = self._get(f"/projects/{project_id}/members", project=project_id)
        if not isinstance(payload, dict):
            return []
        return decode_list(ProjectMember, payload.get("members"))

    def add_member(self, project_id: str, user_id: str, *, role: str = "viewer") -> ProjectMember:
        """Grant ``user_id`` a project role; only owners may manage members."""
        return decode(
            ProjectMember,
            self._post(
                f"/projects/{project_id}/members",
                json={"user_id": user_id, "role": role},
                project=project_id,
            ),
        )

    def update_member(
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

    def remove_member(self, project_id: str, user_id: str) -> bool:
        """Deactivate a member; the project owner cannot be removed."""
        payload = self._delete(f"/projects/{project_id}/members/{user_id}", project=project_id)
        return isinstance(payload, dict) and payload.get("removed") is True

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
    "ProjectFilesAPI",
    "ProjectImportsAPI",
    "ProjectRevisionsAPI",
    "ProjectReworkTasksAPI",
    "ProjectsAPI",
    "WorkspacesAPI",
]
