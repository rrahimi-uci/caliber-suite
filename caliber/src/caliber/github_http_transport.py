"""Real GitHub REST transport over ``httpx`` (`P4-E` production wiring).

`github_source_control.py`'s ``GitHubTransport`` protocol exists precisely
so tests never need one of these -- every offline test in
``test_github_source_control.py`` injects a fake. This module is the one
implementation that actually talks to GitHub, used only when a real,
credential-backed provider is wired into the Workspace source registry
(see ``github_workspace_provider.py``). It never appears in a unit test's
import graph for anything except its own smoke tests, which point at a
local stub server rather than the real network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx

from caliber.github_source_control import GitHubResponse, GitHubTransportError

DEFAULT_TIMEOUT_SECONDS = 15.0


def api_base_url(host: str) -> str:
    """The REST API base URL for a GitHub host.

    github.com's API is served from the separate ``api.github.com`` host;
    GitHub Enterprise Server exposes the same REST surface under its own
    host at ``/api/v3``.
    """
    normalized = (host or "").strip().lower()
    if not normalized or normalized == "github.com":
        return "https://api.github.com"
    return f"https://{normalized}/api/v3"


class HTTPXGitHubTransport:
    """``GitHubTransport`` backed by a real ``httpx.Client``.

    One instance is safe to reuse across requests (and is meant to be --
    the caller is expected to cache one per host rather than build a fresh
    ``httpx.Client``, and its connection pool, on every call).
    """

    def __init__(
        self,
        *,
        host: str = "github.com",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=api_base_url(host), timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> GitHubResponse:
        try:
            response = self._client.request(
                method,
                path,
                headers=dict(headers),
                params=dict(params) if params else None,
                json=dict(json_body) if json_body is not None else None,
            )
        except httpx.HTTPError as exc:
            # Translate every transport-level failure (DNS, TLS, timeout,
            # connection reset) into the adapter's own typed error rather
            # than letting an httpx exception type leak past this module's
            # boundary -- callers only ever need to handle
            # ``GitHubSourceControlError`` subclasses.
            raise GitHubTransportError(
                f"{GitHubTransportError.code}: GitHub request failed: {exc.__class__.__name__}"
            ) from exc
        try:
            payload: object = response.json() if response.content else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}
        return GitHubResponse(
            status_code=response.status_code,
            payload=payload,
            headers=dict(response.headers),
        )

    def close(self) -> None:
        """Release the underlying connection pool, if this instance owns it."""
        if self._owns_client:
            self._client.close()


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "HTTPXGitHubTransport", "api_base_url"]
