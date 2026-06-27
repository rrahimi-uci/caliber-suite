"""Tests for workflow manifest schema migration registry."""

from __future__ import annotations

import pytest

from caliber.workflows import manifest_migrate
from caliber.workflows.manifest import UnsupportedSchemaVersionError


def test_register_migration_rejects_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manifest_migrate, "_MIGRATIONS", {})

    @manifest_migrate.register_migration(1)
    def _first(data: dict) -> dict:
        return data

    with pytest.raises(RuntimeError, match="already registered"):

        @manifest_migrate.register_migration(1)
        def _second(data: dict) -> dict:
            return data


def test_migrate_requires_integer_schema_version() -> None:
    with pytest.raises(UnsupportedSchemaVersionError, match="must be an integer"):
        manifest_migrate.migrate({"schema_version": "1"})


def test_migrate_rejects_future_schema_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manifest_migrate, "CURRENT_SCHEMA_VERSION", 2)
    with pytest.raises(UnsupportedSchemaVersionError, match="newer than the supported"):
        manifest_migrate.migrate({"schema_version": 3})


def test_migrate_reports_gap_in_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manifest_migrate, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(manifest_migrate, "_MIGRATIONS", {})

    with pytest.raises(UnsupportedSchemaVersionError, match="no registered migration"):
        manifest_migrate.migrate({"schema_version": 1})


def test_migrate_walks_chain_without_mutating_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manifest_migrate, "CURRENT_SCHEMA_VERSION", 3)
    monkeypatch.setattr(manifest_migrate, "_MIGRATIONS", {})

    @manifest_migrate.register_migration(1)
    def _one_to_two(data: dict) -> dict:
        data["steps"] = [*data.get("steps", []), "v2"]
        return data

    @manifest_migrate.register_migration(2)
    def _two_to_three(data: dict) -> dict:
        data["steps"] = [*data.get("steps", []), "v3"]
        return data

    original = {"schema_version": 1, "name": "old"}
    migrated = manifest_migrate.migrate(original)

    assert original == {"schema_version": 1, "name": "old"}
    assert migrated["schema_version"] == 3
    assert migrated["steps"] == ["v2", "v3"]
