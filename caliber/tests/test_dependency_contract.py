"""Declared dependency ranges must not admit majors the code cannot run on.

A range is a promise about what a *clean* install may resolve to, and CI is the
only place that promise is tested — developer virtualenvs hold whatever they
installed months ago. That asymmetry has now produced the same outage four times
(see §12.3 of `docs/reports/product-completeness-report.md`): green locally,
broken on the remote, with a diff that mentions nothing related.

These tests read the declaration rather than the environment, so they fail in the
pull request that widens a range instead of in the merge that follows it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - only on the 3.10 floor
    import tomli as tomllib

CALIBER_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = CALIBER_ROOT / "pyproject.toml"

#: Dependency -> the first major this code has **not** been migrated to, with the
#: reason a reviewer needs when a bot proposes widening the range.
#:
#: ``mcp`` 2.0 is a breaking rename, not a drop-in: ``CallToolResult.isError``
#: became ``is_error``, ``read_timeout_seconds`` changed from ``timedelta`` to
#: ``float``, a 2-tuple return grew to 3, ``mcp.server.fastmcp`` moved, and
#: ``streamable_http_client`` switched to an ``httpx2`` client. The last of those
#: changes the HTTP stack, so adopting it needs testing against live MCP servers.
UNMIGRATED_MAJORS: dict[str, tuple[int, str]] = {
    "mcp": (2, "2.0 renames the client API and switches the HTTP stack"),
}


def _runtime_dependencies() -> dict[str, str]:
    parsed = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for spec in parsed["project"]["dependencies"]:
        name = re.split(r"[<>=!~\[]", spec, maxsplit=1)[0].strip().lower()
        found[name] = spec
    return found


def test_no_runtime_range_admits_an_unmigrated_major() -> None:
    """The cap has to exclude the major, not merely predate it.

    ``mcp>=1.27,<3`` looked conservative and admitted 2.0.0 the day it shipped.
    An upper bound is only protection if it sits at or below the first major the
    code has not been ported to.
    """
    dependencies = _runtime_dependencies()
    problems: list[str] = []

    for name, (blocked_major, why) in UNMIGRATED_MAJORS.items():
        spec = dependencies.get(name)
        assert spec, f"{name} is no longer a runtime dependency; drop it from UNMIGRATED_MAJORS"

        upper = re.search(r"<\s*(\d+)(?:\.(\d+))?", spec)
        assert upper, f"{name} declares no upper bound: {spec!r}"

        cap_major = int(upper.group(1))
        if cap_major > blocked_major:
            problems.append(
                f"{spec!r} admits {name} {blocked_major}.x, which this code cannot run: "
                f"{why}. Cap at <{blocked_major} until the migration is done and tested."
            )

    assert not problems, "\n".join(problems)
