"""Projects and their files — the workspace-scoping surface.

Every other resource can be scoped to a project via the ``X-CALIBER-Project``
header, which the client sets for you. This module manages the projects
themselves and the files they hold.
"""

from __future__ import annotations

from typing import Any, BinaryIO

from ..models._decode import decode, decode_list
from ..models.core import Project, ProjectFile, ProjectFolder
from ._base import Resource


class ProjectFilesAPI(Resource):
    """Files inside one project."""

    def list(self, project_id: str) -> tuple[list[ProjectFile], list[ProjectFolder]]:
        """Files and the directories containing them.

        Returned as a pair rather than one flattened list: a directory is not a
        file, and collapsing them would make an empty folder indistinguishable
        from a missing one.
        """
        payload = self._get(f"/projects/{project_id}/files")
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
            "POST", f"/projects/{project_id}/files", files=files, data=data
        )
        return decode(ProjectFile, response.data)

    def create_folder(self, project_id: str, path: str) -> ProjectFolder:
        return decode(
            ProjectFolder, self._post(f"/projects/{project_id}/folders", json={"path": path})
        )

    def delete(self, project_id: str, file_id: str) -> bool:
        payload = self._delete(f"/projects/{project_id}/files/{file_id}")
        return isinstance(payload, dict) and payload.get("status") == "deleted"

    def download(self, project_id: str, file_id: str) -> bytes:
        """Raw bytes. Not JSON, so it bypasses the envelope entirely."""
        return self._transport.download(f"/projects/{project_id}/files/{file_id}/content")


class ProjectsAPI(Resource):
    """Projects, plus their file sub-resource."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.files = ProjectFilesAPI(transport)

    def list(self, *, status: str | None = None) -> list[Project]:
        """Active projects by default; pass ``status="all"`` for everything."""
        params = {"status": status} if status else None
        return decode_list(Project, self._get("/projects", params=params))

    def get(self, project_id: str) -> Project:
        return decode(Project, self._get(f"/projects/{project_id}"))

    def create(self, name: str, *, description: str | None = None) -> Project:
        body: dict[str, Any] = {"name": name}
        if description is not None:
            body["description"] = description
        return decode(Project, self._post("/projects", json=body))

    def update(
        self, project_id: str, *, name: str | None = None, description: str | None = None,
        status: str | None = None,
    ) -> Project:
        body: dict[str, Any] = {}
        for key, value in (("name", name), ("description", description), ("status", status)):
            if value is not None:
                body[key] = value
        return decode(Project, self._patch(f"/projects/{project_id}", json=body))

    def storage(self) -> Any:
        """Where project files live, and what else the deployment supports."""
        return self._get("/projects/storage")


__all__ = ["ProjectFilesAPI", "ProjectsAPI"]
