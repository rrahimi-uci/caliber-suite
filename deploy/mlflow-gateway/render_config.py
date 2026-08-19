#!/usr/bin/env python3
"""Render the effective MLflow AI Gateway config for the keys actually present.

The gateway validates its whole endpoint list up front: a ``$VAR`` placeholder
that resolves to nothing makes ``create_app_from_env`` raise, uvicorn exit 1,
and ``restart: unless-stopped`` turn that into a crash loop. One unconfigured
provider therefore takes down every *other* endpoint too, including ones whose
keys are perfectly valid.

That is the wrong failure shape for an optional-provider surface. This filter
keeps the endpoints whose placeholders resolve and drops the ones they don't,
so the gateway serves what it can and says plainly what it skipped.

Reads the source config, writes the rendered one, and reports to stderr:

    render_config.py <source.yaml> <rendered.yaml>

Placeholders are left as ``$VAR`` in the output — the gateway still resolves
them itself. Exits non-zero only when *nothing* is left to serve, since a
gateway with no endpoints cannot do its job and a clear message beats an
opaque restart loop.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

# ``$VAR`` and ``${VAR}``. MLflow accepts both; so must the filter, or an
# endpoint written the second way looks key-less and is silently kept.
_PLACEHOLDER = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def find_placeholders(node: Any) -> set[str]:
    """Collect every environment variable an endpoint subtree refers to."""
    names: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            names |= find_placeholders(value)
    elif isinstance(node, list):
        for item in node:
            names |= find_placeholders(item)
    elif isinstance(node, str):
        for braced, bare in _PLACEHOLDER.findall(node):
            names.add(braced or bare)
    return names


def missing_vars(endpoint: Any) -> list[str]:
    """Names the endpoint needs that are unset or empty in the environment.

    Empty counts as missing: Compose's ``${VAR:-}`` default turns an absent key
    into an empty string, which is exactly the case that crashed the gateway.
    """
    return sorted(n for n in find_placeholders(endpoint) if not os.environ.get(n))


def render(
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, list[str]]]]:
    """Return the config with unconfigured endpoints removed, plus what was cut."""
    endpoints = config.get("endpoints")
    if not isinstance(endpoints, list):
        raise TypeError("gateway config has no 'endpoints' list")

    kept: list[Any] = []
    skipped: list[tuple[str, list[str]]] = []
    for endpoint in endpoints:
        missing = missing_vars(endpoint)
        name = (
            endpoint.get("name", "<unnamed>")
            if isinstance(endpoint, dict)
            else "<unnamed>"
        )
        if missing:
            skipped.append((name, missing))
        else:
            kept.append(endpoint)

    return {**config, "endpoints": kept}, skipped


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <source.yaml> <rendered.yaml>", file=sys.stderr)
        return 2

    import yaml  # Provided by the mlflow install in the image.

    source, destination = argv[1], argv[2]
    with open(source, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    rendered, skipped = render(config)

    for name, missing in skipped:
        print(
            f"[mlflow-gateway] skipping endpoint '{name}': {', '.join(missing)} unset",
            file=sys.stderr,
        )

    if not rendered["endpoints"]:
        names = ", ".join(sorted({n for _, m in skipped for n in m}))
        print(
            "[mlflow-gateway] no endpoints are configured — set at least one "
            f"provider key in the suite-root .env (looked for: {names})",
            file=sys.stderr,
        )
        return 1

    with open(destination, "w", encoding="utf-8") as handle:
        yaml.safe_dump(rendered, handle, sort_keys=False)

    print(
        f"[mlflow-gateway] serving {len(rendered['endpoints'])} endpoint(s): "
        + ", ".join(e.get("name", "<unnamed>") for e in rendered["endpoints"]),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the container
    raise SystemExit(main(sys.argv))
