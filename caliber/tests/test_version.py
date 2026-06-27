"""Smallest possible test — proves the package is importable and exposes ``__version__``.

This test is what makes CI green from commit #1. Every subsequent test builds on
the same import surface.
"""

from __future__ import annotations

import re

import caliber


def test_version_is_exported() -> None:
    assert hasattr(caliber, "__version__")
    assert isinstance(caliber.__version__, str)
    assert caliber.__version__


def test_version_is_pep440() -> None:
    # Subset of PEP 440 that covers what we use: N.N.N, optionally with .devN/.aN/.bN/.rcN.
    pattern = r"^\d+\.\d+\.\d+(?:\.(?:dev|a|b|rc)\d+)?$"
    assert re.match(pattern, caliber.__version__), (
        f"version {caliber.__version__!r} is not a PEP-440 release identifier"
    )
