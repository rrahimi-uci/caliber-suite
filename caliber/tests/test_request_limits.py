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
from caliber.routes.github_webhooks import PATH as GITHUB_WEBHOOK_PATH

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


def _drive_pure(
    middleware: BodySizeLimitMiddleware,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
    *,
    path: str = "/x",
) -> list[dict[str, Any]]:
    """Run one request through ``middleware`` as pure ASGI and return what was sent to the client."""
    sent: list[dict[str, Any]] = []

    async def drive() -> None:
        scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
        queue = iter(chunks)

        async def receive() -> dict[str, Any]:
            try:
                return {"type": "http.request", "body": next(queue), "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await middleware(scope, receive, send)

    asyncio.run(drive())
    return sent


async def _drain_then_ok(scope: Any, receive: Any, send: Any) -> None:
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_a_malformed_content_length_is_not_a_size_and_the_stream_still_counts() -> None:
    middleware = BodySizeLimitMiddleware(_drain_then_ok, max_bytes=LIMIT)
    ok = _drive_pure(middleware, [(b"content-length", b"not-a-number")], [b"x" * 10])
    assert ok[0]["status"] == 200
    refused = _drive_pure(middleware, [(b"content-length", b"not-a-number")], [b"x" * (LIMIT + 1)])
    assert refused[0]["status"] == 413


def test_the_largest_of_repeated_content_length_headers_decides() -> None:
    middleware = BodySizeLimitMiddleware(_drain_then_ok, max_bytes=LIMIT)
    headers = [(b"content-length", b"5"), (b"content-length", str(LIMIT + 1).encode())]
    assert _drive_pure(middleware, headers, [b"x"])[0]["status"] == 413


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_a_configuration_error(limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        BodySizeLimitMiddleware(lambda *_: None, max_bytes=limit)  # type: ignore[arg-type,return-value]


@pytest.mark.parametrize("name", ["multipart_max_bytes", "path_limits"])
def test_a_non_positive_secondary_limit_is_a_configuration_error(name: str) -> None:
    value = 0 if name == "multipart_max_bytes" else {"/x": -1}
    with pytest.raises(ValueError, match="positive"):
        BodySizeLimitMiddleware(lambda *_: None, max_bytes=LIMIT, **{name: value})  # type: ignore[arg-type,return-value]


def test_the_refusal_names_the_documented_reason() -> None:
    assert REASON == "request_too_large"
    assert too_large_body(7) == b'{"reason": "request_too_large", "max_bytes": 7}'


# ---------------------------------------------------------------- the config and the reference profile


def test_the_upload_limit_defaults_above_the_storage_limit_and_is_read_from_the_environment() -> (
    None
):
    default = CaliberConfig.load(environ={})
    assert default.reference_max_upload_bytes == 256 * 1024 * 1024
    assert default.reference_max_upload_bytes > default.workflow_storage.max_upload_bytes
    loaded = CaliberConfig.load(environ={"CALIBER_REFERENCE_MAX_UPLOAD_BYTES": "4096"})
    assert loaded.reference_max_upload_bytes == 4096


def test_the_limit_defaults_to_one_mebibyte_and_is_read_from_the_environment() -> None:
    assert CaliberConfig.load(environ={}).reference_max_body_bytes == 1_048_576
    loaded = CaliberConfig.load(environ={"CALIBER_REFERENCE_MAX_BODY_BYTES": "4096"})
    assert loaded.reference_max_body_bytes == 4096


@pytest.mark.parametrize("value", ["0", "-5"])
@pytest.mark.parametrize(
    "var", ["CALIBER_REFERENCE_MAX_BODY_BYTES", "CALIBER_REFERENCE_MAX_UPLOAD_BYTES"]
)
def test_a_non_positive_configured_limit_is_rejected(var: str, value: str) -> None:
    from caliber.config import ConfigError

    with pytest.raises(ConfigError):
        CaliberConfig.load(environ={var: value})


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
    assert app.user_middleware[0].kwargs == {
        "max_bytes": 2048,
        "multipart_max_bytes": config.reference_max_upload_bytes,
        "path_limits": {GITHUB_WEBHOOK_PATH: config.github_webhook_max_body_bytes},
    }
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


# ---------------------------------------------------------------- routes with a limit of their own

MULTIPART = (b"content-type", b"multipart/form-data; boundary=abc")


def test_a_multipart_request_gets_the_upload_limit_and_everything_else_the_default() -> None:
    middleware = BodySizeLimitMiddleware(_drain_then_ok, max_bytes=LIMIT, multipart_max_bytes=1000)
    assert _drive_pure(middleware, [MULTIPART], [b"x" * 1000])[0]["status"] == 200
    refused = _drive_pure(middleware, [MULTIPART], [b"x" * 1001])
    assert refused[0]["status"] == 413
    assert refused[1]["body"] == too_large_body(1000), "the refusal names the limit that applied"
    assert (
        _drive_pure(middleware, [(b"content-type", b"application/json")], [b"x" * 101])[0]["status"]
        == 413
    )
    declared = _drive_pure(middleware, [MULTIPART, (b"content-length", b"1001")], [])
    assert declared[0]["status"] == 413, "a declared multipart size is held to the upload limit too"
    assert (
        _drive_pure(middleware, [MULTIPART, (b"content-length", b"1000")], [b"x"])[0]["status"]
        == 200
    )


def test_the_content_type_match_is_case_insensitive_and_exact_about_the_type() -> None:
    middleware = BodySizeLimitMiddleware(_drain_then_ok, max_bytes=LIMIT, multipart_max_bytes=1000)
    upper = (b"content-type", b"Multipart/Form-Data; boundary=abc")
    assert _drive_pure(middleware, [upper], [b"x" * 500])[0]["status"] == 200
    other = (b"content-type", b"multipart/mixed; boundary=abc")
    assert _drive_pure(middleware, [other], [b"x" * 500])[0]["status"] == 413


def test_the_default_limit_is_a_floor_for_the_upload_and_path_limits() -> None:
    """A route's own limit below the default never lowers it."""
    middleware = BodySizeLimitMiddleware(
        _drain_then_ok, max_bytes=LIMIT, multipart_max_bytes=10, path_limits={"/hook": 10}
    )
    assert middleware.limit_for({"headers": [MULTIPART]}) == LIMIT
    assert middleware.limit_for({"path": "/hook", "headers": []}) == LIMIT


def test_a_path_with_its_own_limit_gets_it_and_only_that_exact_path() -> None:
    middleware = BodySizeLimitMiddleware(
        _drain_then_ok, max_bytes=LIMIT, path_limits={"/hook": 1000}
    )
    assert _drive_pure(middleware, [], [b"x" * 1000], path="/hook")[0]["status"] == 200
    assert _drive_pure(middleware, [], [b"x" * 1001], path="/hook")[0]["status"] == 413
    assert _drive_pure(middleware, [], [b"x" * 101], path="/hook/other")[0]["status"] == 413


# ---------------------------------------------------------------- the refusal is traceable


def test_a_refusal_echoes_the_callers_request_id_and_logs_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = BodySizeLimitMiddleware(_drain_then_ok, max_bytes=LIMIT)
    with caplog.at_level("WARNING", logger="caliber.request_limits"):
        sent = _drive_pure(
            middleware, [(b"x-request-id", b"req-123"), (b"content-length", b"500")], []
        )
    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"] == b"req-123"
    assert headers[b"connection"] == b"close"
    [record] = caplog.records
    assert "POST /x" in record.getMessage() and "declared 500 bytes" in record.getMessage()
    assert "req-123" in record.getMessage()


def test_a_refusal_without_a_request_id_generates_one() -> None:
    middleware = BodySizeLimitMiddleware(_drain_then_ok, max_bytes=LIMIT)
    sent = _drive_pure(middleware, [(b"content-length", b"500")], [])
    assert dict(sent[0]["headers"])[b"x-request-id"], (
        "a trace id is generated when the caller sent none"
    )


# ---------------------------------------------------------------- a response already under way


def test_a_body_that_overruns_after_the_response_started_does_not_start_a_second_response() -> None:
    """A streaming handler that reads the body after sending its first chunk: the 413 cannot be sent, so
    exactly one ``http.response.start`` goes out (the handler's own) and the handler is told the client left."""
    seen: list[str] = []

    async def streaming(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"start;", "more_body": True})
        while True:
            message = await receive()
            seen.append(message["type"])
            if message["type"] == "http.disconnect":
                return

    sent = _drive_pure(
        BodySizeLimitMiddleware(streaming, max_bytes=LIMIT), [], [b"x" * 60, b"x" * 60, b"x" * 60]
    )
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [200]
    assert [m for m in sent if m.get("status") == 413] == []
    assert seen[-1] == "http.disconnect", "the handler is told the client left, so it stops reading"
