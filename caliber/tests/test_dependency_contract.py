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
#: ``mcp`` 2.0 was the breaking rename this guarded against: ``CallToolResult.isError``
#: became ``is_error``, ``ClientSession``'s ``read_timeout_seconds`` changed from
#: ``timedelta`` to a plain ``float``, ``streamable_http_client``'s 3-tuple yield
#: (read, write, get_session_id) shrank to a 2-tuple, ``mcp.server.fastmcp.FastMCP``
#: moved to ``mcp.server.mcpserver.MCPServer`` (with host/port/stateless_http/
#: json_response moving off its constructor and onto ``run(transport=...)``), and
#: ``streamable_http_client`` switched its ``http_client`` param to a vendored
#: ``httpx2.AsyncClient`` in place of ``httpx.AsyncClient``. All of that is now
#: migrated (``mcp_gateway.py``, ``mcp_servers/db/server.py``) and covered by
#: ``test_mcp_gateway_units.py``, ``test_mcp_db_tools.py``, and
#: ``test_mcp_db_main.py`` against the actual 2.x package. The cap moves to
#: block 3 instead, on the same "migrate deliberately, don't ride a bot bump"
#: principle -- a future 3.x is unverified until it ships and gets the same
#: treatment.
UNMIGRATED_MAJORS: dict[str, tuple[int, str]] = {
    "mcp": (
        3,
        "3.0 is unreleased and unverified; migrate 2.x -> 3.x deliberately, as this did 1.x -> 2.x",
    ),
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
