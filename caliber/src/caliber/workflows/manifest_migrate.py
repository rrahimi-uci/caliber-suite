"""Manifest schema-version migration registry (plan §26.2).

Each schema version has a registered upgrade function ``vN -> vN+1``. Migrations
are **lossless**: an old manifest can always be upgraded to the current schema.
The compiler accepts only the current version, so :func:`migrate` is the single
choke point that brings any stored manifest up to date on load.

Published (immutable) versions store their *original* ``schema_version``; the
compiler migrates at compile time, not at storage time, so the stored bytes
never change under a workflow that was already approved.

Adding a new schema version
---------------------------
1. Bump ``CURRENT_SCHEMA_VERSION`` in :mod:`caliber.workflows.manifest`.
2. Register a ``_migrate_N_to_N_plus_1`` function here keyed by ``N``.
3. Add a golden round-trip fixture + test (plan §19.13).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from caliber.workflows.manifest import CURRENT_SCHEMA_VERSION, UnsupportedSchemaVersionError

# Map of from-version -> migration function producing the next version. The
# chain is walked until the manifest reaches ``CURRENT_SCHEMA_VERSION``.
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_migration(
    from_version: int,
) -> Callable[
    [Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], dict[str, Any]]
]:
    """Decorator registering a ``from_version -> from_version+1`` migration."""

    def _wrap(
        func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        if from_version in _MIGRATIONS:
            raise RuntimeError(f"migration from v{from_version} already registered")
        _MIGRATIONS[from_version] = func
        return func

    return _wrap


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade ``data`` to :data:`CURRENT_SCHEMA_VERSION`.

    Returns a deep copy; the input is never mutated. Raises
    :class:`UnsupportedSchemaVersionError` for a future version or a gap in the
    migration chain.
    """
    result = deepcopy(data)
    raw_version = result.get("schema_version")
    if not isinstance(raw_version, int):
        raise UnsupportedSchemaVersionError(
            f"manifest schema_version must be an integer, got {raw_version!r}"
        )

    if raw_version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"manifest schema_version {raw_version} is newer than the supported "
            f"version {CURRENT_SCHEMA_VERSION}; upgrade caliber"
        )

    version = raw_version
    while version < CURRENT_SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise UnsupportedSchemaVersionError(
                f"no registered migration from schema_version {version}"
            )
        result = migration(result)
        result["schema_version"] = version + 1
        version += 1

    return result


__all__ = ["migrate", "register_migration"]
