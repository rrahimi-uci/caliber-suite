"""Asynchronous project, workspace, and project-file operations.

The route and model surface intentionally mirrors
``caliber_sdk.resources.projects``. The transport remains the only thing that
changes: every operation awaits the same request, envelope, retry, and project
scope machinery used by the rest of the async client.
"""

from __future__ import annotations

import warnings
from typing import Any, BinaryIO

from ..models._decode import decode, decode_list
from ..models.core import Project, ProjectFile, ProjectFolder, ProjectMember, WorkspaceEnvironment
from ._base import _AsyncResource
from .transport import AsyncTransport

_List = list


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


class AsyncProjectsAPI(_AsyncResource):
    """Projects, project access, environments, and their file sub-resource."""

    def __init__(self, transport: AsyncTransport) -> None:
        super().__init__(transport)
        self.files = AsyncProjectFilesAPI(transport)

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

__all__ = ["AsyncProjectFilesAPI", "AsyncProjectsAPI", "AsyncWorkspacesAPI"]
