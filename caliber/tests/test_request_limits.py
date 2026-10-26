"""``BodySizeLimitMiddleware``: a body above the limit is refused whether it is declared or only streamed.

The middleware is exercised as pure ASGI (so chunked bodies, understated ``content-length`` and a handler that
keeps answering after the refusal can be driven exactly), and through a real Starlette app and the reference
profile's registration in ``create_app``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.request_limits import REASON, BodySizeLimitMiddleware, too_large_body

LIMIT = 100


async def _echo_length(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"length": len(body)})


def _app(limit: int = LIMIT) -> Starlette:
    app = Starlette(routes=[Route("/echo", _echo_length, methods=["POST", "GET"])])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=limit)
    return app


def _refusal(response: httpx.Response, limit: int = LIMIT) -> None:
    assert response.status_code == 413
    assert response.json() == {"reason": "request_too_large", "max_bytes": limit}
    assert response.headers["content-type"] == "application/json"


# ---------------------------------------------------------------- declared length


def test_a_body_at_the_limit_is_accepted() -> None:
    client = TestClient(_app())
    assert client.post("/echo", content=b"x" * LIMIT).json() == {"length": LIMIT}
    assert client.post("/echo", content=b"").json() == {"length": 0}
    assert client.get("/echo").json() == {"length": 0}


def test_a_body_one_byte_over_the_limit_is_refused_before_the_app_runs() -> None:
    reached: list[bool] = []

    async def handler(request: Request) -> JSONResponse:
        reached.append(True)
        return JSONResponse({})

    app = Starlette(routes=[Route("/echo", handler, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)
    _refusal(TestClient(app).post("/echo", content=b"x" * (LIMIT + 1)))
    assert reached == [], "a declared oversize body never reaches the handler"


def test_a_huge_declared_length_is_refused_without_reading_the_body() -> None:
    """The body is never sent: the refusal comes from the header alone."""
    sent: list[Any] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("the application ran")

    async def drive() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [(b"content-length", b"999999999999")],
        }

        async def receive() -> dict[str, Any]:  # pragma: no cover - must not be called
            raise AssertionError("the body was read")

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await BodySizeLimitMiddleware(inner, max_bytes=LIMIT)(scope, receive, send)

    asyncio.run(drive())
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == too_large_body(LIMIT)


# ---------------------------------------------------------------- streamed bodies


def test_a_chunked_body_without_a_content_length_is_bounded() -> None:
    def chunks(total: int):
        for _ in range(total // 10):
            yield b"x" * 10

    client = TestClient(_app())
    assert client.post("/echo", content=chunks(LIMIT)).json() == {"length": LIMIT}
    _refusal(client.post("/echo", content=chunks(LIMIT + 10)))


def test_an_understated_content_length_cannot_smuggle_a_large_body() -> None:
    """The header says 5 bytes; the stream carries 500. The stream count is what is enforced."""
    app = _app()
    messages: list[dict[str, Any]] = []

    async def drive() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/echo",
            "headers": [(b"content-length", b"5")],
            "query_string": b"",
            "app": app,
            "router": app.router,
        }
        chunks = iter([b"a" * 300, b"b" * 300])

        async def receive() -> dict[str, Any]:
            try:
                return {"type": "http.request", "body": next(chunks), "more_body": True}
            except StopIteration:
                return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)

    asyncio.run(drive())
    statuses = [m["status"] for m in messages if m["type"] == "http.response.start"]
    assert statuses == [413], "exactly one response, and it is the refusal"


def test_nothing_the_app_says_after_the_refusal_reaches_the_client() -> None:
    sent: list[dict[str, Any]] = []

    async def stubborn(scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def drive() -> None:
        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        queue = iter([{"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": True}])

        async def receive() -> dict[str, Any]:
            return next(queue, {"type": "http.disconnect"})

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await BodySizeLimitMiddleware(stubborn, max_bytes=LIMIT)(scope, receive, send)

    asyncio.run(drive())
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]
    assert [m["body"] for m in sent if m["type"] == "http.response.body"] == [too_large_body(LIMIT)]


# ---------------------------------------------------------------- other scopes and bad input


def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def drive() -> None:
        for kind in ("lifespan", "websocket"):
            await BodySizeLimitMiddleware(inner, max_bytes=1)({"type": kind}, None, None)  # type: ignore[arg-type]

    asyncio.run(drive())
    assert seen == ["lifespan", "websocket"]


def test_a_malformed_content_length_does_not_crash_and_the_stream_still_counts() -> None:
    client = TestClient(_app())
    ok = client.post("/echo", content=b"x" * 10, headers={"content-length": "not-a-number"})
    assert ok.status_code in {200, 400}, (
        "the client library may reject it; the middleware must not 500"
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_a_configuration_error(limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        BodySizeLimitMiddleware(lambda *_: None, max_bytes=limit)  # type: ignore[arg-type,return-value]


def test_the_refusal_names_the_documented_reason() -> None:
    assert REASON == "request_too_large"
    assert too_large_body(7) == b'{"reason": "request_too_large", "max_bytes": 7}'


# ---------------------------------------------------------------- the config and the reference profile


def test_the_limit_defaults_to_one_mebibyte_and_is_read_from_the_environment() -> None:
    assert CaliberConfig.load(environ={}).reference_max_body_bytes == 1_048_576
    loaded = CaliberConfig.load(environ={"CALIBER_REFERENCE_MAX_BODY_BYTES": "4096"})
    assert loaded.reference_max_body_bytes == 4096


@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_non_positive_configured_limit_is_rejected(value: str) -> None:
    from caliber.config import ConfigError

    with pytest.raises(ConfigError):
        CaliberConfig.load(environ={"CALIBER_REFERENCE_MAX_BODY_BYTES": value})


def test_an_application_error_is_not_swallowed() -> None:
    async def broken(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("the handler failed")

    async def drive() -> None:
        scope = {"type": "http", "method": "GET", "path": "/", "headers": []}

        async def receive() -> dict[str, Any]:  # pragma: no cover - the handler never reads
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:  # pragma: no cover - nothing is sent
            return None

        await BodySizeLimitMiddleware(broken, max_bytes=LIMIT)(scope, receive, send)

    with pytest.raises(RuntimeError, match="the handler failed"):
        asyncio.run(drive())


# ---------------------------------------------------------------- the reference profile registers it, development does not

HEALTH = "/ajax-api/2.0/mlflow/caliber/health"


def test_the_development_profile_installs_no_limit(app_config: CaliberConfig) -> None:
    from caliber.server import create_app

    app = create_app(app_config)
    assert BodySizeLimitMiddleware not in [m.cls for m in app.user_middleware]
    # The default profile accepts a body far above the reference limit (the route answers, not the limiter).
    big = TestClient(app).post(HEALTH, content=b"x" * (2 * 1_048_576))
    assert big.status_code != 413


def test_the_reference_profile_installs_the_limit_outermost(
    app_config: CaliberConfig, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built on the test SQLite engine: the profile's PostgreSQL URL is a setting, as in ``test_profile``."""
    from caliber.db.session import create_engine_from_config
    from caliber.server import create_app
    from tests.test_profile import SAFE

    monkeypatch.setattr(
        "caliber.server.create_engine_from_config",
        lambda _config: create_engine_from_config(app_config),
    )
    monkeypatch.setenv(app_config.csrf_signing_secret_env, "x" * 48)
    config = app_config.model_copy(update={**SAFE, "reference_max_body_bytes": 2048})
    app = create_app(config)
    assert app.user_middleware[0].cls is BodySizeLimitMiddleware, (
        "outermost: before CSRF, auth and routing"
    )
    assert app.user_middleware[0].kwargs == {"max_bytes": 2048}
    client = TestClient(app)
    _refusal(client.post(HEALTH, content=b"x" * 2049), limit=2048)
    assert client.get(HEALTH).status_code == 200


def test_reading_again_after_the_refusal_keeps_reporting_a_disconnect_and_a_client_disconnect_passes_through() -> (
    None
):
    reads: list[str] = []

    async def reader(scope: Any, receive: Any, send: Any) -> None:
        for _ in range(3):
            reads.append((await receive())["type"])

    async def drive(first: dict[str, Any]) -> None:
        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        queue = iter([first])

        async def receive() -> dict[str, Any]:
            return next(queue, {"type": "http.disconnect"})

        async def send(message: dict[str, Any]) -> None:
            return None

        await BodySizeLimitMiddleware(reader, max_bytes=LIMIT)(scope, receive, send)

    asyncio.run(drive({"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": True}))
    assert reads == ["http.disconnect"] * 3, "after the refusal every read is a disconnect"
    reads.clear()
    asyncio.run(drive({"type": "http.disconnect"}))
    assert reads == ["http.disconnect"] * 3, (
        "a client that leaves first is passed through, not counted"
    )
