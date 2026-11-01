"""CLI entry point for the standalone CALIBER tool sandbox service."""

from __future__ import annotations

import argparse
import importlib
from types import ModuleType

from caliber.config import CaliberConfig
from caliber.effects.refusals import refuse_in_reference_profile

try:
    uvicorn_mod: ModuleType | None = importlib.import_module("uvicorn")
except ImportError:  # pragma: no cover - depends on optional extra.
    uvicorn_mod = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CALIBER tool sandbox service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()

    # The service has no authentication, so the reference profile does not start it (EP-029, B04.7, #774). Refused before
    # anything binds a port, from the same configuration the service itself would load.
    refuse_in_reference_profile("EP-029", CaliberConfig.load())

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
