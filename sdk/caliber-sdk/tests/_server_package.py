"""Import modules of the server package for the fake servers that run its real code.

The SDK is its own distribution and does not depend on the server, so the server's own
third-party dependencies (``pydantic``, ``sqlalchemy``, ...) are not in the SDK's dev extras. A fake
server that wants the real data transform or the real sandbox namespace therefore imports them
through :func:`server_module`: with the server's dependencies installed (the repository's dev
environment) it returns the real module, and without them the importing test module is skipped
with the missing dependency named, instead of failing at collection.

A server module that has been renamed or removed is not a missing dependency and still raises.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

SERVER_SRC = Path(__file__).resolve().parents[3] / "caliber" / "src"


def server_module(name: str) -> Any:
    """The server module ``name``, typed ``Any`` (the server is not on the SDK's mypy path)."""
    if str(SERVER_SRC) not in sys.path:
        sys.path.append(str(SERVER_SRC))
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is None or exc.name == "caliber" or exc.name.startswith("caliber."):
            raise
        pytest.skip(
            f"needs {exc.name!r}, a dependency of the server package that the SDK's dev extras "
            "do not install",
            allow_module_level=True,
        )
