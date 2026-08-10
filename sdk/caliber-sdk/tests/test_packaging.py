"""Packaging guarantees, asserted rather than assumed.

The reason ``caliber-sdk`` is a separate distribution is that installing it
must not drag in the CALIBER server. That is a property of the metadata, so it
is checked against the metadata rather than trusted to review.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ``tomllib`` is stdlib only from 3.11, and this package supports 3.10 -- a floor
# that :func:`test_the_supported_python_floor_matches_the_server` asserts right
# here. A bare ``import tomllib`` made that assertion untestable on the very
# version it was about: the module failed to import on 3.10, and mypy (configured
# for 3.10) resolved typeshed without it and reported import-not-found.
if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - only taken on the 3.10 floor
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

#: Packages whose presence would defeat the point of a separate distribution.
FORBIDDEN_RUNTIME_DEPS = ("mlflow", "sqlalchemy", "starlette", "alembic", "pydantic")


def _config() -> dict[str, Any]:
    parsed: dict[str, Any] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return parsed


def test_runtime_dependencies_stay_minimal() -> None:
    """Two runtime dependencies, and a named reason to add a third.

    ``pydantic`` is on the forbidden list deliberately: it is not a server
    dependency, but adopting it here would pull a compiled core into every
    developer script for models that dataclasses already express.
    """
    dependencies = _config()["project"]["dependencies"]
    names = [dep.split(">")[0].split("<")[0].split("=")[0].strip().lower() for dep in dependencies]
    assert names == ["httpx", "typing-extensions"], names
    for forbidden in FORBIDDEN_RUNTIME_DEPS:
        assert not any(forbidden in name for name in names), f"{forbidden} leaked into runtime deps"


def test_the_package_ships_type_information() -> None:
    """``py.typed`` is what makes the annotations visible to a consumer."""
    assert (ROOT / "src" / "caliber_sdk" / "py.typed").is_file()
    assert _config()["project"]["classifiers"], "classifiers should advertise Typing :: Typed"
    assert "Typing :: Typed" in _config()["project"]["classifiers"]


def test_the_wheel_includes_only_the_sdk_package() -> None:
    packages = _config()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["src/caliber_sdk"]


def test_the_supported_python_floor_matches_the_server() -> None:
    """A developer on the server's floor must be able to install the client."""
    assert _config()["project"]["requires-python"] == ">=3.10"


def test_examples_are_not_shipped_in_the_wheel() -> None:
    """They are test fixtures and documentation sources, not library code.

    Shipping them would put an ``examples`` top-level package into every
    consumer's environment, which is a name collision waiting to happen.
    """
    packages = _config()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert not any("examples" in package for package in packages)
