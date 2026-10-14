"""Every distribution's declared version agrees with itself, and with the rest.

The UI's ``package.json`` is included (alpha gap P0-7): it was ``0.1.0`` while the four Python
packages were ``0.1.0a1``, so the release read as two different versions. npm needs semver, so
``0.1.0a1`` is spelled ``0.1.0-alpha.1`` there; ``test_the_ui_package_declares_the_same_release``
translates one into the other rather than comparing strings.

Phase 5.5 of sdk-completeness-plan.md asks for a "server/SDK compatibility
matrix, published and tested." There is no historical matrix to publish yet
(see sdk/caliber-sdk/VERSIONING.md's "Server / SDK compatibility" section) --
``caliber``, ``caliber-sdk``, ``caliber-cli``, and ``caliber-plugin-sdk`` are
released in lockstep from this one repository, all currently ``0.1.0a1``.
That lockstep claim is exactly the kind of thing that silently rots the
moment one package's version is bumped and another is forgotten, so it is a
test rather than only a sentence in a doc: each package declares its version
twice (``pyproject.toml`` for packaging, a ``__version__`` constant for
runtime introspection), and both copies, across all four packages, must agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]

#: name -> (pyproject.toml, the file declaring __version__)
DISTRIBUTIONS: dict[str, tuple[Path, Path]] = {
    "caliber": (
        REPO_ROOT / "caliber" / "pyproject.toml",
        REPO_ROOT / "caliber" / "src" / "caliber" / "__init__.py",
    ),
    "caliber-sdk": (
        REPO_ROOT / "sdk" / "caliber-sdk" / "pyproject.toml",
        REPO_ROOT / "sdk" / "caliber-sdk" / "src" / "caliber_sdk" / "__init__.py",
    ),
    "caliber-cli": (
        REPO_ROOT / "sdk" / "caliber-cli" / "pyproject.toml",
        REPO_ROOT / "sdk" / "caliber-cli" / "src" / "caliber_cli" / "cli.py",
    ),
    "caliber-plugin-sdk": (
        REPO_ROOT / "sdk" / "caliber-plugin-sdk" / "pyproject.toml",
        REPO_ROOT / "sdk" / "caliber-plugin-sdk" / "src" / "caliber_plugin_sdk" / "__init__.py",
    ),
}

_VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _pyproject_version(path: Path) -> str:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _dunder_version(path: Path) -> str:
    """Read ``__version__`` by regex rather than importing the module.

    ``caliber-cli`` and ``caliber-plugin-sdk`` are not installed into this
    package's own virtualenv -- and should not need to be, just to check a
    version string -- so this reads the source text directly, the same way
    ``docs-site/generate_sdk_docs.py`` already parses these packages with
    ``ast`` rather than importing them.
    """
    match = _VERSION_ASSIGNMENT.search(path.read_text(encoding="utf-8"))
    assert match, f'no `__version__ = "..."` assignment found in {path}'
    return match.group(1)


def test_every_distribution_declares_a_consistent_version() -> None:
    """Two copies of the truth (packaging metadata, runtime constant) for
    each of four independently-versioned packages -- eight numbers that
    must currently read as one, because the compatibility statement in
    VERSIONING.md depends on it."""
    versions: dict[str, tuple[str, str]] = {}
    for name, (pyproject_path, source_path) in DISTRIBUTIONS.items():
        assert pyproject_path.is_file(), f"{name}: missing {pyproject_path}"
        assert source_path.is_file(), f"{name}: missing {source_path}"
        versions[name] = (_pyproject_version(pyproject_path), _dunder_version(source_path))

    mismatched = {
        name: (pkg_version, dunder_version)
        for name, (pkg_version, dunder_version) in versions.items()
        if pkg_version != dunder_version
    }
    assert not mismatched, (
        f"pyproject.toml version and __version__ disagree within a package: {mismatched}"
    )

    distinct = {pkg_version for pkg_version, _dunder in versions.values()}
    assert len(distinct) == 1, (
        "caliber, caliber-sdk, caliber-cli, and caliber-plugin-sdk are released "
        f"in lockstep (see VERSIONING.md) but disagree: {versions}"
    )


_PEP440_PRE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?:(?P<kind>a|b|rc)(?P<n>\d+))?$")
_SEMVER_KIND = {"a": "alpha", "b": "beta", "rc": "rc"}


def _semver_from_pep440(version: str) -> str:
    """``0.1.0a1`` -> ``0.1.0-alpha.1``; a final release is unchanged."""
    match = _PEP440_PRE.match(version)
    assert match, f"cannot translate {version!r}: only X.Y.Z and X.Y.ZaN/bN/rcN are supported"
    if match["kind"] is None:
        return match["base"]
    return f"{match['base']}-{_SEMVER_KIND[match['kind']]}.{match['n']}"


def test_the_pep440_to_semver_translation() -> None:
    assert _semver_from_pep440("0.1.0a1") == "0.1.0-alpha.1"
    assert _semver_from_pep440("0.1.0b2") == "0.1.0-beta.2"
    assert _semver_from_pep440("1.2.3rc4") == "1.2.3-rc.4"
    assert _semver_from_pep440("1.2.3") == "1.2.3"


def test_the_ui_package_declares_the_same_release() -> None:
    """``package.json`` and the lockfile's root entry both name the release the wheels carry."""
    import json

    ui = REPO_ROOT / "caliber" / "caliber-ui"
    expected = _semver_from_pep440(_pyproject_version(REPO_ROOT / "caliber" / "pyproject.toml"))
    package = json.loads((ui / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == expected, (
        f"caliber-ui/package.json is {package['version']!r}; the release is {expected!r}"
    )
    lock = json.loads((ui / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == expected
    assert lock["packages"][""]["version"] == expected
