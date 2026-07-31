"""Unit tests for shared route helpers in ``caliber.routes._deps``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from caliber.routes._deps import list_limit, parse_json_object


def _request(**params: str) -> Any:
    """A minimal Request stand-in exposing only ``.query_params.get``."""
    return SimpleNamespace(query_params=dict(params))


def _streaming_request(
    chunks: list[bytes],
    *,
    content_length: str | None = None,
) -> tuple[Request, dict[str, int]]:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    state = {"receive_calls": 0}

    async def receive() -> dict[str, object]:
        state["receive_calls"] += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    headers = [] if content_length is None else [(b"content-length", content_length.encode())]
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    return Request(scope, receive), state  # type: ignore[arg-type]


def test_list_limit_defaults_when_absent() -> None:
    assert list_limit(_request()) == (500, 0)


def test_list_limit_reads_explicit_values() -> None:
    assert list_limit(_request(limit="25", offset="10")) == (25, 10)


def test_list_limit_caps_oversized_limit() -> None:
    assert list_limit(_request(limit="999999")) == (2000, 0)


def test_list_limit_floors_limit_at_one() -> None:
    # A request for zero/negative rows still returns at least one — a list
    # endpoint that returned nothing for ``?limit=0`` would look broken.
    assert list_limit(_request(limit="0")) == (1, 0)
    assert list_limit(_request(limit="-5")) == (1, 0)


def test_list_limit_clamps_negative_offset_to_zero() -> None:
    assert list_limit(_request(offset="-3")) == (500, 0)


@pytest.mark.parametrize("bad", ["abc", "", "1.5", "ten"])
def test_list_limit_falls_back_on_garbage(bad: str) -> None:
    # Garbage values must not 500 the endpoint — they fall back to defaults.
    assert list_limit(_request(limit=bad, offset=bad)) == (500, 0)


def test_list_limit_honors_custom_default_and_cap() -> None:
    assert list_limit(_request(), default=50, cap=100) == (50, 0)
    assert list_limit(_request(limit="500"), default=50, cap=100) == (100, 0)


@pytest.mark.asyncio
async def test_parse_json_object_accepts_exact_streaming_body_limit() -> None:
    raw = b'{"a":1}'
    request, state = _streaming_request([raw[:3], raw[3:]])

    assert await parse_json_object(request, max_body_bytes=len(raw)) == {"a": 1}
    assert state["receive_calls"] == 2


@pytest.mark.asyncio
async def test_parse_json_object_rejects_declared_oversize_without_reading() -> None:
    request, state = _streaming_request([b'{"a":1}'], content_length="8")

    with pytest.raises(HTTPException) as raised:
        await parse_json_object(request, max_body_bytes=7)

    assert raised.value.status_code == 413
    assert state["receive_calls"] == 0


@pytest.mark.asyncio
async def test_parse_json_object_counts_chunks_despite_understated_length() -> None:
    request, state = _streaming_request(
        [b'{"a":', b'"too long"}'],
        content_length="1",
    )

    with pytest.raises(HTTPException) as raised:
        await parse_json_object(request, max_body_bytes=8)

    assert raised.value.status_code == 413
    assert state["receive_calls"] == 2
