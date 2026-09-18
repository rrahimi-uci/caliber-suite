"""Client construction, credential precedence, and discovery calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import httpx
import pytest

from caliber_sdk import CaliberClient, CaliberConfigError
from caliber_sdk.client import ENV_BASE_URL, ENV_PROJECT, ENV_TOKEN, ENV_USER

BASE = "https://caliber.test"


def client_with(handler: object, **kwargs: object) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return CaliberClient(BASE, http_client=http, **kwargs)  # type: ignore[arg-type]


def test_base_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    with pytest.raises(CaliberConfigError):
        CaliberClient()


def test_configuration_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CI script should not have to thread config through its own parsing."""
    monkeypatch.setenv(ENV_BASE_URL, BASE)
    monkeypatch.setenv(ENV_TOKEN, "calpat_env")
    monkeypatch.setenv(ENV_PROJECT, "PRJ-env")

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with CaliberClient(http_client=http) as caliber:
        caliber.whoami()
    assert seen["authorization"] == "Bearer calpat_env"
    assert seen["x-caliber-project"] == "PRJ-env"


def test_a_token_beats_a_trusted_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token is a real credential; the header is only an assertion."""
    monkeypatch.setenv(ENV_USER, "@from-env")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {}})

    with client_with(handler, token="calpat_explicit") as caliber:
        caliber.whoami()
    assert seen["authorization"] == "Bearer calpat_explicit"
    assert "x-caliber-user" not in seen


def test_discovery_calls_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("openapi.json"):
            return httpx.Response(200, json={"openapi": "3.0.3", "paths": {}})
        return httpx.Response(200, json={"data": {"sdk_stability": {"ga": ["prompts"]}}})

    with client_with(handler) as caliber:
        assert caliber.capabilities()["sdk_stability"]["ga"] == ["prompts"]
        assert caliber.openapi()["openapi"] == "3.0.3"
        caliber.whoami()
        caliber.health()

    assert [path.rsplit("/", 1)[-1] for path in seen] == [
        "capabilities",
        "openapi.json",
        "me",
        "health",
    ]


def test_stability_reports_the_servers_tiers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"sdk_stability": {"ga": ["prompts"], "beta": []}}
        return httpx.Response(200, json={"data": payload})

    with client_with(handler) as caliber:
        assert caliber.stability["ga"] == ["prompts"]


def test_stability_is_empty_when_the_server_omits_it() -> None:
    """An older server predates the field; that is not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    with client_with(handler) as caliber:
        assert caliber.stability == {}


def test_project_scope_applies_and_restores_the_project_header() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-caliber-project"))
        return httpx.Response(200, json={"data": {}})

    with client_with(handler, project="PRJ-original") as caliber:
        caliber.whoami()
        with caliber.project_scope(" PRJ-created "):
            caliber.whoami()
        caliber.whoami()

    assert seen == ["PRJ-original", "PRJ-created", "PRJ-original"]


def test_workspace_and_library_scopes_are_explicit_and_nested() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-caliber-project"))
        return httpx.Response(200, json={"data": {}})

    with client_with(handler, project="PRJ-original") as caliber:
        assert caliber.workspaces is caliber.projects
        with caliber.workspace_scope(" PRJ-workspace "):
            caliber.whoami()
            with caliber.library_scope():
                caliber.whoami()
            caliber.whoami()
        caliber.whoami()

    assert seen == ["PRJ-workspace", None, "PRJ-workspace", "PRJ-original"]


def test_project_scope_restores_context_after_an_error() -> None:
    caliber = client_with(lambda _request: httpx.Response(200), project="PRJ-original")
    try:
        with pytest.raises(RuntimeError, match="stop"), caliber.project_scope("PRJ-created"):
            raise RuntimeError("stop")
        assert caliber._transport.project == "PRJ-original"
    finally:
        caliber.close()


def test_project_scope_is_context_local_across_threads() -> None:
    """Concurrent users of one client must not overwrite each other's scope."""
    seen: list[str | None] = []
    lock = Lock()
    entered = Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append(request.headers.get("x-caliber-project"))
        return httpx.Response(200, json={"data": {}})

    def request_in_scope(caliber: CaliberClient, project_id: str) -> None:
        with caliber.project_scope(project_id):
            entered.wait(timeout=5)
            caliber.whoami()
            entered.wait(timeout=5)

    with (
        client_with(handler, project="PRJ-default") as caliber,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [
            executor.submit(request_in_scope, caliber, project_id)
            for project_id in ("PRJ-A", "PRJ-B")
        ]
        for future in futures:
            future.result(timeout=5)

    assert sorted(seen, key=lambda project_id: project_id or "") == ["PRJ-A", "PRJ-B"]


def test_project_scope_rejects_an_empty_project_id() -> None:
    caliber = client_with(lambda _request: httpx.Response(200))
    try:
        with pytest.raises(CaliberConfigError, match="project_id"), caliber.project_scope("  "):
            pass
    finally:
        caliber.close()


def test_projects_resource_pins_the_header_to_the_path_project_id() -> None:
    """§13.4: a `/projects/{project_id}/...` request must send *that* id, not
    whatever the client happens to be ambiently scoped to -- a client
    configured for one project asking about a different one by id must not
    silently claim to be the first project."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-caliber-project"))
        return httpx.Response(200, json={"data": {"project_id": "PRJ-B", "name": "B"}})

    with client_with(handler, project="PRJ-A") as caliber:
        caliber.projects.get("PRJ-B")

    assert seen == ["PRJ-B"]


def test_projects_resource_pin_does_not_leak_into_the_next_call() -> None:
    """The per-call pin in :meth:`ProjectsAPI.get` must not mutate the
    client's ambient scope for calls that follow it."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-caliber-project"))
        return httpx.Response(200, json={"data": {"project_id": "x", "name": "n"}})

    with client_with(handler, project="PRJ-A") as caliber:
        caliber.projects.get("PRJ-B")
        caliber.whoami()

    assert seen == ["PRJ-B", "PRJ-A"]
