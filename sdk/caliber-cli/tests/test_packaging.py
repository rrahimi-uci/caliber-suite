"""The CLI's dependency on the SDK is bounded (alpha gap P1-18).

``dependencies = ["caliber-sdk"]`` was satisfiable by anything published under that name,
and nothing was, so installing the CLI alone was unsatisfiable. An unbounded, unclaimed name
is also a dependency-confusion risk the day an index is used.
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

CLI = Path(__file__).resolve().parents[1] / "pyproject.toml"
SDK = Path(__file__).resolve().parents[2] / "caliber-sdk" / "pyproject.toml"


def _cli_dependencies() -> list[str]:
    """The CLI's ``dependencies`` list, read without a TOML parser (``tomllib`` is 3.11+)."""
    text = CLI.read_text(encoding="utf-8")
    block = re.search(r"^dependencies = \[(.*?)^\]", text, re.S | re.M)
    assert block, "dependencies list not found"
    return re.findall(r'^\s*"([^"]+)"', block.group(1), re.M)


def _sdk_version() -> str:
    match = re.search(r'^version = "([^"]+)"', SDK.read_text(encoding="utf-8"), re.M)
    assert match, "sdk version not found"
    return match.group(1)


def test_the_cli_requires_a_bounded_sdk() -> None:
    requirements = [Requirement(item) for item in _cli_dependencies()]
    sdk = next(r for r in requirements if r.name == "caliber-sdk")

    assert str(sdk.specifier), "caliber-sdk must carry a version bound"
    assert any(s.operator in {"<", "<="} for s in sdk.specifier), "it must have an upper bound"
    assert any(s.operator in {">=", "=="} for s in sdk.specifier), "it must have a lower bound"


def test_the_sdk_in_this_repository_satisfies_the_clis_requirement() -> None:
    requirements = [Requirement(item) for item in _cli_dependencies()]
    sdk = next(r for r in requirements if r.name == "caliber-sdk")
    version = Version(_sdk_version())

    assert sdk.specifier.contains(version, prereleases=True), (
        f"caliber-cli requires caliber-sdk{sdk.specifier} but the repository's SDK is {version}"
    )


def test_the_readme_installs_from_wheels_not_an_index_name() -> None:
    readme = (CLI.parent / "README.md").read_text(encoding="utf-8")
    assert "pip install --find-links ./wheels caliber-cli" in readme
    assert "\npip install caliber-cli\n" not in readme
