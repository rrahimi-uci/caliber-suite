"""Smallest possible test — proves the package is importable and exposes ``__version__``.

This test is what makes CI green from commit #1. Every subsequent test builds on
the same import surface.
"""

from __future__ import annotations

import pytest
from packaging.version import InvalidVersion, Version

import caliber


def test_version_is_exported() -> None:
    assert hasattr(caliber, "__version__")
    assert isinstance(caliber.__version__, str)
    assert caliber.__version__


def test_version_is_pep440() -> None:
    # ``packaging`` is the reference PEP 440 implementation (and a pytest
    # dependency, so it is always present under the dev extras). A hand-rolled
    # regex here once rejected the valid alpha ``0.1.0a1`` because it demanded
    # a dot before the pre-release tag, which PEP 440 reserves for ``.devN``
    # and ``.postN`` only.
    try:
        parsed = Version(caliber.__version__)
    except InvalidVersion as exc:
        pytest.fail(f"version {caliber.__version__!r} is not PEP 440: {exc}")
    # Require the canonical spelling so ``0.1.0.a1`` or ``v0.1.0`` cannot ship
    # even though ``packaging`` would normalise them.
    assert str(parsed) == caliber.__version__, (
        f"version {caliber.__version__!r} is not in canonical PEP 440 form ({str(parsed)!r})"
    )


@pytest.mark.parametrize(
    ("candidate", "canonical"),
    [
        ("0.1.0", True),
        ("0.1.0a1", True),
        ("0.1.0b2", True),
        ("0.1.0rc1", True),
        ("0.1.0.dev3", True),
        ("0.1.0.post1", True),
        ("0.1.0.a1", False),
        ("v0.1.0", False),
        ("0.1.0-alpha", False),
    ],
)
def test_pep440_canonical_check_examples(candidate: str, canonical: bool) -> None:
    # Pin down what the check above accepts and rejects so a future release
    # string is judged by the same rule the real test applies.
    try:
        parsed = Version(candidate)
    except InvalidVersion:
        assert not canonical
        return
    assert (str(parsed) == candidate) is canonical
