"""Client-side late binding for CALIBER prompt aliases.

``PromptResolver`` is intended to run inside an agent process. It asks CALIBER
which immutable prompt version an alias names, caches the resolved template for a
short TTL, and serves the last known value when the control plane is temporarily
unreachable. The agent still calls its model provider directly; CALIBER is only
on the alias-resolution path.

Fallback is deliberately conservative: an outage can reuse a value that was
successfully resolved before, but it can never invent a version or turn a first
resolution failure into an empty prompt.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import quote

import httpx

ResolutionSource = Literal["control_plane", "cache", "last_known"]
TelemetrySink = Callable[[Mapping[str, Any]], None]


class PromptResolutionError(RuntimeError):
    """The requested alias has no usable control-plane or last-known value."""


@dataclass(frozen=True)
class ResolvedPrompt:
    """One immutable prompt selected by a mutable alias."""

    name: str
    alias: str
    version: int
    template: str
    artifact_ref: str
    source: ResolutionSource
    stale: bool
    age_seconds: float


@dataclass(frozen=True)
class _CacheEntry:
    value: ResolvedPrompt
    fetched_at: float


class PromptResolver:
    """Resolve aliases with TTL caching and last-known fallback.

    Parameters
    ----------
    api_url:
        CALIBER API root, normally
        ``https://host/ajax-api/2.0/mlflow/caliber``.
    headers:
        Authentication headers appropriate for the deployment. Values are never
        included in telemetry.
    ttl_seconds:
        Fresh-cache lifetime. Set to zero to contact CALIBER on every call while
        retaining outage fallback.
    max_stale_seconds:
        Optional ceiling on last-known fallback age. ``None`` keeps the last
        known value until a newer resolution succeeds.
    telemetry:
        Optional best-effort callback. Callback failures never break resolution.
    client:
        Injectable ``httpx.Client`` for custom transports and tests. The caller
        owns an injected client; otherwise the resolver owns and closes it.
    """

    def __init__(
        self,
        api_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        ttl_seconds: float = 30.0,
        max_stale_seconds: float | None = None,
        timeout_seconds: float = 2.0,
        telemetry: TelemetrySink | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")
        if max_stale_seconds is not None and max_stale_seconds < 0:
            raise ValueError("max_stale_seconds must be >= 0 or None")
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._max_stale_seconds = (
            float(max_stale_seconds) if max_stale_seconds is not None else None
        )
        self._telemetry = telemetry
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._api_url = api_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers=dict(headers or {}),
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PromptResolver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def clear(self, name: str | None = None, alias: str | None = None) -> None:
        """Clear all cached resolutions, or the selected name/alias subset."""
        with self._lock:
            if name is None and alias is None:
                self._cache.clear()
                return
            self._cache = {
                key: entry
                for key, entry in self._cache.items()
                if not ((name is None or key[0] == name) and (alias is None or key[1] == alias))
            }

    def resolve(self, name: str, alias: str = "prod") -> ResolvedPrompt:
        name = name.strip()
        alias = alias.strip()
        if not name or not alias:
            raise ValueError("name and alias must be non-empty")
        key = (name, alias)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            age = max(0.0, now - cached.fetched_at)
            if age <= self._ttl_seconds:
                self._emit("cache_hit", name=name, alias=alias, age_seconds=age)
                return replace(cached.value, source="cache", stale=False, age_seconds=age)

        started = self._clock()
        try:
            response = self._client.get(
                f"{self._api_url}/prompts/{quote(name, safe='')}",
                params={"alias": alias},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("response does not contain an object 'data' envelope")
            version = data.get("version")
            template = data.get("template")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValueError("resolved prompt version is not a positive integer")
            if not isinstance(template, str) or not template:
                raise ValueError("resolved prompt template is empty")
            value = ResolvedPrompt(
                name=str(data.get("name") or name),
                alias=str(data.get("alias") or alias),
                version=version,
                template=template,
                artifact_ref=str(data.get("artifact_ref") or f"prompts:/{name}@{alias}"),
                source="control_plane",
                stale=False,
                age_seconds=0.0,
            )
            fetched_at = self._clock()
            with self._lock:
                self._cache[key] = _CacheEntry(value=value, fetched_at=fetched_at)
            self._emit(
                "resolved",
                name=name,
                alias=alias,
                version=version,
                latency_ms=max(0.0, (fetched_at - started) * 1000),
            )
            return value
        except Exception as exc:
            fallback_now = self._clock()
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                age = max(0.0, fallback_now - cached.fetched_at)
                if self._max_stale_seconds is None or age <= self._max_stale_seconds:
                    self._emit(
                        "last_known_fallback",
                        name=name,
                        alias=alias,
                        version=cached.value.version,
                        age_seconds=age,
                        error_type=type(exc).__name__,
                    )
                    return replace(
                        cached.value,
                        source="last_known",
                        stale=True,
                        age_seconds=age,
                    )
            self._emit(
                "resolution_failed",
                name=name,
                alias=alias,
                error_type=type(exc).__name__,
            )
            raise PromptResolutionError(
                f"could not resolve prompt {name!r} alias {alias!r} and no usable "
                "last-known value exists"
            ) from exc

    def _emit(self, event: str, **fields: Any) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry({"event": event, **fields})
        except Exception:
            # Telemetry is a side channel, never a serving-path dependency.
            return
