from __future__ import annotations

import httpx
import pytest

from caliber.resolver import PromptResolutionError, PromptResolver


def _prompt(version: int = 4, template: str = "Be helpful") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "name": "support-agent",
                "alias": "prod",
                "version": version,
                "template": template,
                "artifact_ref": "prompts:/support-agent@prod",
            }
        },
    )


def test_resolver_uses_ttl_cache_without_recontacting_control_plane() -> None:
    calls = 0
    now = [100.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url == (
            "https://caliber.test/ajax-api/2.0/mlflow/caliber/prompts/support-agent?alias=prod"
        )
        return _prompt()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = PromptResolver(
        "https://caliber.test/ajax-api/2.0/mlflow/caliber/",
        client=client,
        ttl_seconds=30,
        clock=lambda: now[0],
    )

    first = resolver.resolve("support-agent")
    now[0] += 10
    second = resolver.resolve("support-agent")

    assert first.source == "control_plane"
    assert second.source == "cache" and not second.stale
    assert second.version == 4
    assert calls == 1


def test_resolver_serves_last_known_value_during_outage() -> None:
    calls = 0
    now = [100.0]
    events: list[dict[str, object]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _prompt(version=7, template="Known good")
        raise httpx.ConnectError("control plane unavailable")

    client = httpx.Client(base_url="https://caliber.test", transport=httpx.MockTransport(handler))
    resolver = PromptResolver(
        "https://unused.test",
        client=client,
        ttl_seconds=5,
        clock=lambda: now[0],
        telemetry=lambda event: events.append(dict(event)),
    )
    resolver.resolve("support-agent")
    now[0] += 6

    fallback = resolver.resolve("support-agent")

    assert fallback.source == "last_known" and fallback.stale
    assert fallback.version == 7 and fallback.template == "Known good"
    assert events[-1]["event"] == "last_known_fallback"
    assert "template" not in events[-1]


def test_resolver_fails_closed_without_a_known_good_value() -> None:
    client = httpx.Client(
        base_url="https://caliber.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    resolver = PromptResolver("https://unused.test", client=client)

    with pytest.raises(PromptResolutionError, match="no usable last-known"):
        resolver.resolve("never-seen")


def test_resolver_honors_maximum_stale_age() -> None:
    calls = 0
    now = [0.0]

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _prompt() if calls == 1 else httpx.Response(503)

    client = httpx.Client(base_url="https://caliber.test", transport=httpx.MockTransport(handler))
    resolver = PromptResolver(
        "https://unused.test",
        client=client,
        ttl_seconds=1,
        max_stale_seconds=5,
        clock=lambda: now[0],
    )
    resolver.resolve("support-agent")
    now[0] = 6

    with pytest.raises(PromptResolutionError):
        resolver.resolve("support-agent")


def test_telemetry_failure_never_breaks_resolution() -> None:
    client = httpx.Client(
        base_url="https://caliber.test",
        transport=httpx.MockTransport(lambda _request: _prompt()),
    )

    def broken_telemetry(_event: object) -> None:
        raise RuntimeError("collector down")

    resolver = PromptResolver(
        "https://unused.test",
        client=client,
        telemetry=broken_telemetry,
    )
    assert resolver.resolve("support-agent").version == 4
