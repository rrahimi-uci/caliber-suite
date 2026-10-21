"""Feature flags for the areas the alpha does not stand behind.

``docs/start/alpha-scope.md`` splits the product into what the alpha supports and what
it does not: preview areas, a record-only area and an experimental one. Marking them in
the UI is honest, but it still ships them. A deployment that wants a clean alpha turns
one **off**: each area below is hidden by the SPA and refused by the API while its flag
is off.

Four flags, each covering the routes of one area:

* ``aria`` -- the Aria assistant and plans. Unlike the other three, this one ships **on**
  by default; set ``CALIBER_FEATURE_ARIA=false`` for a deployment that wants it off.
* ``integrations`` -- MCP servers and OpenAPI integrations. Off by default.
* ``cookbooks`` -- the guided cookbook labs. Off by default.
* ``release_candidates`` -- release-candidate signoff and workspace releases. Off by
  default. The live board, the timeline and release operations are core and stay on.

A flag gates by **path prefix** in one ASGI middleware rather than inside each route:
the gate cannot be forgotten on a new route in a gated area, a route added there is
refused until someone decides otherwise, and the routes stay registered, so the OpenAPI
document, the route inventories and the SDK coverage gate describe the same surface in
every deployment. A disabled call answers ``404`` with ``reason_code: feature_disabled``
and the variable to set, so a client can tell "this build does not have it on" from "no
such record".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from caliber.config import CaliberConfig

PREFIX: Final[str] = "/ajax-api/2.0/mlflow/caliber"

#: ``reason_code`` of the 404 a disabled area returns.
REASON_CODE: Final[str] = "feature_disabled"


@dataclass(frozen=True)
class FeatureFlag:
    """One gated area."""

    id: str
    env_var: str
    config_field: str
    label: str
    #: ``preview`` or ``record_only``; the alpha scope note defines both.
    stability: str
    #: Path prefixes under :data:`PREFIX`. A path matches a prefix exactly or when it
    #: continues with ``/``, ``:`` or ``.`` (``/x/1``, ``/x/1:apply``), never as a
    #: longer word (``/cookbooks-extra`` is not ``/cookbooks``).
    prefixes: tuple[str, ...]
    #: Suffix patterns for per-project routes (``/projects/{id}/releases``), which a
    #: plain prefix cannot express. Each entry is the literal segment after the id.
    project_segments: tuple[str, ...] = ()


FEATURES: Final[tuple[FeatureFlag, ...]] = (
    FeatureFlag(
        id="aria",
        env_var="CALIBER_FEATURE_ARIA",
        config_field="feature_aria_enabled",
        label="Aria assistant and plans",
        stability="preview",
        prefixes=("/assistant", "/aria"),
    ),
    FeatureFlag(
        id="integrations",
        env_var="CALIBER_FEATURE_INTEGRATIONS",
        config_field="feature_integrations_enabled",
        label="MCP servers and OpenAPI integrations",
        stability="preview",
        prefixes=("/mcp-servers", "/openapi-integrations"),
    ),
    FeatureFlag(
        id="cookbooks",
        env_var="CALIBER_FEATURE_COOKBOOKS",
        config_field="feature_cookbooks_enabled",
        label="Cookbooks",
        stability="preview",
        prefixes=("/cookbooks",),
    ),
    FeatureFlag(
        id="release_candidates",
        env_var="CALIBER_FEATURE_RELEASE_CANDIDATES",
        config_field="feature_release_candidates_enabled",
        label="Release-candidate signoff and workspace releases",
        stability="record_only",
        prefixes=("/releases/candidates", "/releases/report-jobs"),
        project_segments=("releases",),
    ),
)

_BY_ID: Final[dict[str, FeatureFlag]] = {flag.id: flag for flag in FEATURES}

#: ``/projects/{id}/<segment>``: split gives ['', 'projects', id, segment, ...].
_PROJECT_SEGMENT_INDEX: Final[int] = 3


def feature_enabled(config: CaliberConfig, feature_id: str) -> bool:
    """Whether the area ``feature_id`` is on for this deployment."""
    return bool(getattr(config, _BY_ID[feature_id].config_field))


def _matches(path: str, prefix: str) -> bool:
    if not path.startswith(prefix):
        return False
    rest = path[len(prefix) :]
    return rest == "" or rest[0] in "/:."


def feature_for_path(path: str) -> FeatureFlag | None:
    """The gated area that owns ``path``, or ``None`` for an ungated path."""
    if not path.startswith(PREFIX + "/"):
        return None
    relative = path[len(PREFIX) :]
    for flag in FEATURES:
        if any(_matches(relative, prefix) for prefix in flag.prefixes):
            return flag
        if flag.project_segments and relative.startswith("/projects/"):
            parts = relative.split("/")  # ['', 'projects', '{id}', 'releases', ...]
            if (
                len(parts) > _PROJECT_SEGMENT_INDEX
                and parts[_PROJECT_SEGMENT_INDEX].split(":")[0] in flag.project_segments
            ):
                return flag
    return None


def disabled_message(flag: FeatureFlag) -> str:
    return f"{flag.label} is disabled in this deployment. Set {flag.env_var}=true to enable it."


def capabilities_block(config: CaliberConfig) -> dict[str, dict[str, Any]]:
    """The ``features`` object of ``GET /capabilities``: every flag, on or off."""
    return {
        flag.id: {
            "enabled": feature_enabled(config, flag.id),
            "env_var": flag.env_var,
            "label": flag.label,
            "stability": flag.stability,
        }
        for flag in FEATURES
    }


class FeatureGateMiddleware:
    """Refuse requests to a disabled area before they reach auth, CSRF or a handler.

    Raw ASGI, like the other middlewares, so it adds no per-request cost beyond a prefix
    check on API paths. It reads the flag from ``app.state.config`` on every request, so a
    test (or an embedding application) that swaps the config sees the change.
    """

    def __init__(self, app: Any, *, config: CaliberConfig) -> None:
        self._app = app
        self._config = config

    def _live_config(self, scope: dict[str, Any]) -> CaliberConfig:
        """``app.state.config`` when present, else the config this was built with."""
        state = getattr(scope.get("app"), "state", None)
        live = getattr(state, "config", None)
        return live if live is not None else self._config

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            flag = feature_for_path(str(scope.get("path", "")))
            if flag is not None and not feature_enabled(self._live_config(scope), flag.id):
                await _refuse(flag, send)
                return
        await self._app(scope, receive, send)


async def _refuse(flag: FeatureFlag, send: Any) -> None:
    body = json.dumps(
        {
            "detail": disabled_message(flag),
            "status_code": 404,
            "reason_code": REASON_CODE,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "FEATURES",
    "PREFIX",
    "REASON_CODE",
    "FeatureFlag",
    "FeatureGateMiddleware",
    "capabilities_block",
    "disabled_message",
    "feature_enabled",
    "feature_for_path",
]
