"""CLI entry point for the standalone CALIBER tool sandbox service."""

from __future__ import annotations

import argparse
import importlib
from types import ModuleType

try:
    uvicorn_mod: ModuleType | None = importlib.import_module("uvicorn")
except ImportError:  # pragma: no cover - depends on optional extra.
    uvicorn_mod = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CALIBER tool sandbox service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()

    if uvicorn_mod is None:
        raise SystemExit(
            "uvicorn is required to run the sandbox service. "
            "Install with: pip install -e '.[sandbox]'"
        )

    uvicorn_mod.run(
        "caliber.tool_sandbox.server:create_app", factory=True, host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
