"""Client construction, credential precedence, and discovery calls."""

from __future__ import annotations

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
