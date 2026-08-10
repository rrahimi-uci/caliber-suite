"""A CLI driven against a stubbed transport, with real argument parsing.

``main()`` builds its own client from the environment, which is exactly the
behaviour under test — so the tests set the environment and patch the client's
HTTP transport rather than injecting a fake client. Everything from ``argv`` to
the exit code is the real code path.
"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from caliber_sdk import CaliberClient

import caliber_cli.cli as cli_main

BASE = "https://caliber.test"

#: A stub route: "METHOD /path" -> a response body, or a callable taking the
#: request and returning one.
Routes = dict[str, Any]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test must never inherit a real deployment's credentials.

    Autouse because a single test that forgot this could talk to whatever
    ``CALIBER_BASE_URL`` happens to be set to on a developer's machine.
    """
    for name in ("CALIBER_BASE_URL", "CALIBER_TOKEN", "CALIBER_PROJECT", "CALIBER_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CALIBER_BASE_URL", BASE)
    monkeypatch.setenv("CALIBER_TOKEN", "calpat_test")


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., list[str]]]:
    """Install stub routes and return a runner: ``run(*argv) -> exit code``."""
    recorded: list[str] = []

    def install(routes: Routes) -> Callable[..., int]:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.rsplit("/caliber", 1)[-1]
            key = f"{request.method} {path}"
            recorded.append(key)
            if key not in routes:
                return httpx.Response(404, json={"detail": f"no stub for {key}"})
            body = routes[key]
            if callable(body):
                body = body(request)
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json={"data": body})

        # Taken from ``caliber_sdk``, where it is public, rather than read back
        # off the CLI module -- the CLI imports it privately, and reaching for it
        # there would need a type-ignore to say so.
        def patched(*args: Any, **kwargs: Any) -> Any:
            kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler))
            return CaliberClient(*args, **kwargs)

        monkeypatch.setattr(cli_main, "CaliberClient", patched)
        return cli_main.main

    yield install  # type: ignore[misc]


@pytest.fixture
def calls() -> list[str]:
    return []


def body_of(request: httpx.Request) -> Any:
    return jsonlib.loads(request.content) if request.content else None
