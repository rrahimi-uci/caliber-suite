"""CLI entrypoint: ``python -m caliber.mcp_servers.db --mode <mode>``.

The CALIBER MCP catalog spawns this over stdio. ``POSTGRES_URL`` must be set in
the environment the server inherits (the gateway resolves the ``${POSTGRES_URL}``
placeholder from the CALIBER process env when launching the subprocess).
"""

from __future__ import annotations

import argparse
import os

from caliber.mcp_servers.db.server import MODES, build_server


def parse_mode(argv: list[str] | None = None) -> str:
    """Resolve the requested mode from argv, falling back to ``CALIBER_DB_MODE``."""
    parser = argparse.ArgumentParser(prog="python -m caliber.mcp_servers.db")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=os.environ.get("CALIBER_DB_MODE", "relational"),
        help="Which database class to expose (default: relational, or $CALIBER_DB_MODE).",
    )
    return str(parser.parse_args(argv).mode)


def main(argv: list[str] | None = None) -> int:
    build_server(parse_mode(argv)).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
