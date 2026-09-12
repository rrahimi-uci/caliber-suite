"""Projects and their files — the workspace-scoping surface.

Every other resource can be scoped to a project via the ``X-CALIBER-Project``
header, which the client sets for you. This module manages the projects
themselves and the files they hold.
"""

from __future__ import annotations

import warnings
from typing import Any, BinaryIO

from ..models._decode import decode, decode_list
from ..models.core import Project, ProjectFile, ProjectFolder, ProjectMember
from ._base import Resource

_List = list


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
    """Projects, project access, and the file sub-resource."""

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
            result = decode(Project, self._patch(f"/projects/{project_id}", json=body))
        return result

    def archive(self, project_id: str) -> Project:
        """Move a project to the ``archived`` status, recording who/when."""
        return decode(Project, self._post(f"/projects/{project_id}/archive"))

    def restore(self, project_id: str) -> Project:
        """Move an archived project back to ``active``, clearing provenance."""
        return decode(Project, self._post(f"/projects/{project_id}/restore"))

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
            ),
        )

    def list_members(self, project_id: str) -> _List[ProjectMember]:
        """List active members and their effective project roles."""
        payload = self._get(f"/projects/{project_id}/members")
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
            self._patch(f"/projects/{project_id}/members/{user_id}", json=body),
        )

    def remove_member(self, project_id: str, user_id: str) -> bool:
        """Deactivate a member; the project owner cannot be removed."""
        payload = self._delete(f"/projects/{project_id}/members/{user_id}")
        return isinstance(payload, dict) and payload.get("removed") is True

    def storage(self) -> Any:
        """Where project files live, and what else the deployment supports."""
        return self._get("/projects/storage")


__all__ = ["ProjectFilesAPI", "ProjectsAPI"]
