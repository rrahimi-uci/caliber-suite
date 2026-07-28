"""CLI entrypoint for the bundled database MCP server.

The server supports local stdio and a stateless streamable-HTTP sidecar.  The
shipped deployment uses one container per mode so relational, vector, and graph
tools keep separate process boundaries. ``POSTGRES_URL`` must be set.
"""

from __future__ import annotations

import argparse
import os
from typing import Literal, cast

from caliber.mcp_servers.db.server import MODES, build_server

TRANSPORTS = ("stdio", "sse", "streamable-http")
MAX_PORT = 65_535


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m caliber.mcp_servers.db")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=os.environ.get("CALIBER_DB_MODE", "relational"),
        help="Which database class to expose (default: relational, or $CALIBER_DB_MODE).",
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=os.environ.get("CALIBER_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("CALIBER_MCP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("CALIBER_MCP_PORT", "8000")),
    )
    return parser


def parse_mode(argv: list[str] | None = None) -> str:
    """Resolve the requested mode from argv, falling back to ``CALIBER_DB_MODE``."""
    return str(_parser().parse_args(argv).mode)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= MAX_PORT:
        raise SystemExit("--port must be between 1 and 65535")
    transport = cast(Literal["stdio", "sse", "streamable-http"], str(args.transport))
    build_server(str(args.mode), host=str(args.host), port=int(args.port)).run(transport=transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
