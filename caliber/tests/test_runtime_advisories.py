from __future__ import annotations

from importlib import metadata as importlib_metadata

from caliber.runtime_advisories import (
    get_runtime_dependency_advisories,
    version_at_most,
    version_below,
    version_tuple_prefix,
)


def test_version_tuple_prefix_ignores_suffixes() -> None:
    assert version_tuple_prefix("1.83.10-stable") == (1, 83, 10)


def test_version_at_most_pads_shorter_versions() -> None:
    assert version_at_most("2.12", (2, 12, 0)) is True
    assert version_at_most("2.12.1", (2, 12, 0)) is False


def test_version_below_handles_stable_suffixes() -> None:
    assert version_below("1.83.7-stable", (1, 83, 10)) is True
    assert version_below("1.83.10-stable", (1, 83, 10)) is False


def test_get_runtime_dependency_advisories_flags_documented_ranges(monkeypatch) -> None:
    versions = {
        "diskcache": "5.6.2",
        "torch": "2.11.0",
        "litellm": "1.83.7-stable",
    }

    monkeypatch.setattr(importlib_metadata, "version", versions.__getitem__)

    advisories = get_runtime_dependency_advisories()

    assert [(advisory.package_name, advisory.installed_version) for advisory in advisories] == [
        ("diskcache", "5.6.2"),
        ("torch", "2.11.0"),
        ("litellm", "1.83.7-stable"),
    ]


def test_get_runtime_dependency_advisories_skips_safe_or_missing_versions(monkeypatch) -> None:
    versions = {
        "diskcache": "5.6.4",
        "torch": "2.12.1",
        "litellm": "1.83.10",
    }

    def _version(name: str) -> str:
        if name in versions:
            return versions[name]
        raise importlib_metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib_metadata, "version", _version)

    assert get_runtime_dependency_advisories() == []
