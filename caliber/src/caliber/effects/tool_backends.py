"""Which registered tool sends through which backend, as the operator declared it (#1051).

A governed agent step's proposal names a connector that is the model's word; the platform ties it to the agent through a map
of *registry identity to backend kind* (:attr:`caliber.effects.step_proposal.GovernedStepContext.tool_backends`). That map must come
from trusted configuration, never from a manifest, a tool call or a model, so it has one source: ``CALIBER_AGENT_STEP_TOOL_BACKENDS``,
a JSON array of ``{"tool": <registry name>, "version": <registry version>, "backend": <backend kind>}`` rows.

This module is a leaf on purpose (it imports nothing from the platform), so the configuration model can validate the setting at
startup without importing the effect modules that import the configuration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

#: The backend kinds a *model's* proposal may reach (see :mod:`caliber.effects.step_proposal`). Each has a destination the platform
#: checks at proposal time and an authoritative lookup by key the platform can call (``caliber.effects.reconcile.CONNECTORS``),
#: so an ambiguous outcome can be settled. ``http``, ``registered_tool`` and ``openapi_http`` are not here: they have neither.
AGENT_STEP_BACKENDS: Final = frozenset({"document_request"})

_KEYS: Final = frozenset({"tool", "version", "backend"})
#: A registry name or version: printable, no whitespace or control character, bounded (the registry columns are shorter).
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}")


def parse_tool_backends(raw: str) -> Mapping[tuple[str, str], str]:
    """The ``(tool name, tool version) -> backend kind`` map in ``raw`` (a JSON array), or ``ValueError`` saying what is wrong.

    Every row has exactly the keys ``tool``, ``version`` and ``backend``; the identity is a registry name and version (never a local
    name from a manifest), the backend is one of :data:`AGENT_STEP_BACKENDS`, and an identity is mapped once. An empty array maps
    nothing: no agent step is governed, which is the default.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("agent_step_tool_backends must be a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError("agent_step_tool_backends must be a JSON array")
    mapping: dict[tuple[str, str], str] = {}
    for item in parsed:
        if not isinstance(item, dict) or set(item) != _KEYS:
            raise ValueError(f"each row has exactly the keys {sorted(_KEYS)}")
        for key in ("tool", "version"):
            value = item[key]
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{key} must be a registry name or version (printable, no spaces)")
        backend = item["backend"]
        if backend not in AGENT_STEP_BACKENDS:
            raise ValueError(f"backend must be one of {sorted(AGENT_STEP_BACKENDS)}")
        identity = (item["tool"], item["version"])
        if identity in mapping:
            raise ValueError(f"tool {identity[0]!r} version {identity[1]!r} is mapped twice")
        mapping[identity] = backend
    return mapping


__all__ = ["AGENT_STEP_BACKENDS", "parse_tool_backends"]
