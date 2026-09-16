"""Async project/workspace resource parity and wire-contract tests."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import httpx
import pytest

from caliber_sdk.aio import AsyncCaliberClient, AsyncProjectFilesAPI, AsyncProjectsAPI
from caliber_sdk.resources.projects import ProjectFilesAPI, ProjectsAPI

BASE = "https://caliber.test"


def envelope(data: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"data": data})


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def client_with(handler: Any) -> AsyncCaliberClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncCaliberClient(BASE, token="calpat_test", http_client=http)


def _route_path(request: httpx.Request) -> str:
    return request.url.path.rsplit("/caliber", 1)[-1]


def test_async_project_public_signatures_match_the_sync_surface() -> None:
    """Adding ``await`` must not remove a project operation or alter its inputs."""

    def public_methods(cls: type[Any]) -> dict[str, inspect.Signature]:
        return {
            name: inspect.signature(member)
            for name, member in vars(cls).items()
            if not name.startswith("_") and callable(member)
        }

    def comparable(signature: inspect.Signature) -> list[tuple[str, Any, Any, Any]]:
        return [
            (parameter.name, parameter.kind, parameter.default, parameter.annotation)
            for parameter in list(signature.parameters.values())[1:]
        ]

    assert public_methods(AsyncProjectsAPI).keys() == public_methods(ProjectsAPI).keys()
    assert public_methods(AsyncProjectFilesAPI).keys() == public_methods(ProjectFilesAPI).keys()
    for async_cls, sync_cls in (
        (AsyncProjectsAPI, ProjectsAPI),
        (AsyncProjectFilesAPI, ProjectFilesAPI),
    ):
        async_methods = public_methods(async_cls)
        sync_methods = public_methods(sync_cls)
        for name in async_methods:
            assert comparable(async_methods[name]) == comparable(sync_methods[name]), name


def test_async_projects_and_files_cover_the_complete_typed_route_surface() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = _route_path(request)
        seen.append((request.method, path, request.headers.get("x-caliber-project")))
        if path.endswith("/content"):
            return httpx.Response(200, content=b"project-file")
        if path == "/projects/storage":
            return envelope({"backend": "local", "supports_uploads": True})
        if path == "/projects":
            if request.method == "GET":
                assert request.url.params.get("status") == "all"
                return envelope([{"project_id": "PRJ-1", "name": "demo"}])
            return envelope({"project_id": "PRJ-new", "name": "created"}, status_code=201)
        if path == "/projects/PRJ-1":
            return envelope({"project_id": "PRJ-1", "name": "updated"})
        if path.endswith("/archive"):
            return envelope({"project_id": "PRJ-1", "status": "archived"})
        if path.endswith("/restore"):
            return envelope({"project_id": "PRJ-1", "status": "active"})
        if path.endswith("/transfer-ownership"):
            return envelope({"project_id": "PRJ-1", "owner": "@bob"})
        if path.endswith("/files"):
            if request.method == "GET":
                return envelope(
                    {
                        "items": [{"file_id": "F-1", "name": "notes.txt"}],
                        "directories": [{"path": "notes"}],
                    }
                )
            assert request.headers.get("content-type", "").startswith("multipart/form-data")
            assert b"notes.txt" in request.content
            assert b"kind" in request.content
            return envelope({"file_id": "F-1", "name": "notes.txt"}, status_code=201)
        if path.endswith("/folders"):
            return envelope({"path": "notes", "name": "notes"}, status_code=201)
        if "/files/" in path:
            return envelope({"status": "deleted"})
        if path.endswith("/members"):
            if request.method == "GET":
                return envelope({"members": [{"user_id": "@bob", "role": "editor"}]})
            return envelope(
                {"member_id": "PM-1", "user_id": "@bob", "role": "editor"}, status_code=201
            )
        if "/members/" in path:
            if request.method == "DELETE":
                return envelope({"removed": True})
            return envelope({"member_id": "PM-1", "user_id": "@bob", "status": "inactive"})
        if path.endswith("/environments"):
            return envelope({"environments": [{"name": "qa", "status": "active"}]})
        if path.endswith("/enable"):
            return envelope({"name": "qa", "status": "active"})
        if path.endswith("/disable"):
            return envelope({"name": "qa", "status": "disabled"})
        if "/environments/" in path:
            return envelope({"name": "qa", "environment_class": "qa"})
        raise AssertionError(f"unhandled {request.method} {path}")

    async def main() -> dict[str, Any]:
        async with client_with(handler) as caliber:
            listed = await caliber.projects.list(status="all")
            detail = await caliber.projects.get("PRJ-1")
            created = await caliber.projects.create("created", description="desc")
            updated = await caliber.projects.update("PRJ-1", name="updated")
            archived = await caliber.projects.archive("PRJ-1")
            restored = await caliber.projects.restore("PRJ-1")
            transferred = await caliber.projects.transfer_ownership("PRJ-1", "@bob")
            members = await caliber.projects.list_members("PRJ-1")
            added = await caliber.projects.add_member("PRJ-1", "@bob", role="editor")
            changed = await caliber.projects.update_member("PRJ-1", "@bob", status="inactive")
            removed = await caliber.projects.remove_member("PRJ-1", "@bob")
            storage = await caliber.projects.storage()
            environments = await caliber.projects.list_environments("PRJ-1")
            environment = await caliber.projects.get_environment("PRJ-1", "qa")
            enabled = await caliber.projects.enable_environment("PRJ-1", "qa")
            disabled = await caliber.projects.disable_environment("PRJ-1", "qa")
            files, folders = await caliber.projects.files.list("PRJ-1")
            uploaded = await caliber.projects.files.upload(
                "PRJ-1", filename="notes.txt", content=b"hello", kind="evidence"
            )
            folder = await caliber.projects.files.create_folder("PRJ-1", "notes")
            deleted = await caliber.projects.files.delete("PRJ-1", "F-1")
            downloaded = await caliber.projects.files.download("PRJ-1", "F-1")
            return locals()

    values = run(main())
    assert values["listed"][0].project_id == "PRJ-1"
    assert values["detail"].name == "updated"
    assert values["created"].project_id == "PRJ-new"
    assert values["updated"].name == "updated"
    assert values["archived"].status == "archived"
    assert values["restored"].status == "active"
    assert values["transferred"].owner == "@bob"
    assert values["members"][0].role == "editor"
    assert values["added"].member_id == "PM-1"
    assert values["changed"].status == "inactive"
    assert values["removed"] is True
    assert values["storage"]["backend"] == "local"
    assert values["environments"][0].name == "qa"
    assert values["environment"].environment_class == "qa"
    assert values["enabled"].status == "active"
    assert values["disabled"].status == "disabled"
    assert values["files"][0].file_id == "F-1"
    assert values["folders"][0].path == "notes"
    assert values["uploaded"].file_id == "F-1"
    assert values["folder"].path == "notes"
    assert values["deleted"] is True
    assert values["downloaded"] == b"project-file"

    for _method, path, project in seen:
        if path.startswith("/projects/PRJ-1"):
            assert project == "PRJ-1", path


def test_async_project_path_pins_scope_over_ambient_project() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-caliber-project"))
        return envelope({"project_id": "PRJ-path", "name": "path"})

    async def main() -> None:
        async with client_with(handler) as caliber:
            caliber._transport.project = "PRJ-ambient"
            project = await caliber.projects.get("PRJ-path")
            assert project.project_id == "PRJ-path"

    run(main())
    assert seen == ["PRJ-path"]


def test_async_project_update_status_keeps_the_sync_deprecation_contract() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {_route_path(request)}")
        return envelope({"project_id": "PRJ-1", "status": "archived"})

    async def main() -> Any:
        async with client_with(handler) as caliber:
            with pytest.warns(DeprecationWarning):
                return await caliber.projects.update("PRJ-1", status="archived")

    project = run(main())
    assert project.status == "archived"
    assert seen == ["POST /projects/PRJ-1/archive"]


def test_async_project_update_rejects_an_unknown_status_before_io() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return envelope({})

    async def main() -> None:
        async with client_with(handler) as caliber:
            with pytest.raises(ValueError, match="unsupported status"):
                await caliber.projects.update("PRJ-1", status="deleted")

    run(main())
    assert calls == 0


def test_async_client_exposes_projects_as_the_typed_resource() -> None:
    async def main() -> None:
        async with client_with(lambda _request: envelope({})) as caliber:
            assert isinstance(caliber.projects, AsyncProjectsAPI)
            assert isinstance(caliber.projects.files, AsyncProjectFilesAPI)

    run(main())
