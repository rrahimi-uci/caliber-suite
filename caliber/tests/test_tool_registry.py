"""Tool registry resolver tests (plan §19.12)."""

from __future__ import annotations

import pytest

from caliber.workflows.tools import (
    InMemoryToolResolver,
    ToolRegistryEntry,
    ToolResolutionError,
    family_name,
)


def _resolver(entries) -> InMemoryToolResolver:
    return InMemoryToolResolver(entries)


def test_family_name_extraction() -> None:
    assert family_name("tool.lookup_policy.v1") == "lookup_policy"
    assert family_name("lookup_policy") == "lookup_policy"


def test_version_constraint_resolution_picks_highest_match() -> None:
    entries = [
        ToolRegistryEntry(name="x", version="1.0", module_path="m", callable_name="f"),
        ToolRegistryEntry(name="x", version="1.5", module_path="m", callable_name="f"),
        ToolRegistryEntry(name="x", version="2.0", module_path="m", callable_name="f"),
    ]
    res = _resolver(entries).resolve("tool.x.v1", ">=1.0,<2.0")
    assert res.entry.version == "1.5"


def test_no_version_satisfies_constraint() -> None:
    entries = [ToolRegistryEntry(name="x", version="1.0", module_path="m", callable_name="f")]
    with pytest.raises(ToolResolutionError):
        _resolver(entries).resolve("tool.x.v1", ">=3.0")


def test_unregistered_family() -> None:
    with pytest.raises(ToolResolutionError):
        _resolver([]).resolve("tool.ghost.v1")


def test_deprecated_resolves_with_warning() -> None:
    entries = [
        ToolRegistryEntry(
            name="x", version="1.0", module_path="m", callable_name="f", status="deprecated"
        )
    ]
    res = _resolver(entries).resolve("tool.x.v1")
    assert res.entry.version == "1.0"
    assert res.warnings


def test_deprecated_with_successor_recommendation() -> None:
    entries = [
        ToolRegistryEntry(
            name="x",
            version="1.0",
            module_path="m",
            callable_name="f",
            status="deprecated",
            successor_ref="tool.y.v2",
        )
    ]
    res = _resolver(entries).resolve("tool.x.v1")
    assert any("tool.y.v2" in w for w in res.warnings)


def test_active_preferred_over_deprecated_same_constraint() -> None:
    entries = [
        ToolRegistryEntry(
            name="x", version="1.0", module_path="m", callable_name="f", status="deprecated"
        ),
        ToolRegistryEntry(
            name="x", version="1.0", module_path="m", callable_name="f", status="active"
        ),
    ]
    res = _resolver(entries).resolve("tool.x.v1", ">=1.0")
    assert res.entry.status == "active"


def test_archived_excluded() -> None:
    entries = [
        ToolRegistryEntry(
            name="x", version="1.0", module_path="m", callable_name="f", status="archived"
        )
    ]
    with pytest.raises(ToolResolutionError):
        _resolver(entries).resolve("tool.x.v1")


def test_registry_ref_property() -> None:
    entry = ToolRegistryEntry(
        name="lookup_policy", version="1.5", module_path="m", callable_name="f"
    )
    assert entry.registry_ref == "tool.lookup_policy.v1"
