"""Database URL normalization helpers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit


def normalize_database_url(url: str) -> str:
    """Normalize user-facing DSNs to the SQLAlchemy driver we ship.

    Local development and docs often use ``postgresql://`` or ``postgres://``
    as shorthand. This project installs ``psycopg`` rather than ``psycopg2``,
    so normalize the shorthand to SQLAlchemy's explicit ``+psycopg`` driver
    unless the caller already chose a driver.
    """

    normalized = url.strip()
    if normalized.startswith("postgresql+"):
        return normalized
    if normalized.startswith("postgresql://"):
        return f"postgresql+psycopg://{normalized.removeprefix('postgresql://')}"
    if normalized.startswith("postgres://"):
        return f"postgresql+psycopg://{normalized.removeprefix('postgres://')}"
    return normalized


def sqlite_file_path(url: str | None) -> Path | None:
    """The resolved file a ``sqlite:`` URL points at, or ``None``.

    ``None`` for any non-SQLite URL, an in-memory database, or an empty value.
    Query strings (``?timeout=30``) and the driver suffix (``sqlite+pysqlite:``)
    are ignored, and the path is resolved (symlinks included) so ``./a.db`` and an absolute
    spelling of the same file compare equal.
    """
    if not url or not url.strip().lower().startswith("sqlite"):
        return None
    parts = urlsplit(url.strip())
    raw = unquote(parts.path)
    if parts.netloc:  # sqlite://host/path is not a local file URL we can compare
        return None
    # ``sqlite:///rel.db`` has path ``/rel.db`` and ``sqlite:////abs.db`` has
    # ``//abs.db``: SQLAlchemy treats the first slash after ``///`` as a separator.
    raw = raw[1:] if raw.startswith("/") else raw
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        return None
    return Path(raw).resolve()


def shared_sqlite_store(database_url: str | None, *mlflow_uris: str | None) -> Path | None:
    """The SQLite file CALIBER's database shares with an MLflow store, if any.

    MLflow's tracking/registry/backend-store URIs are checked against the CALIBER
    database URL. Returns the shared file, or ``None`` when there is no overlap.
    """
    own = sqlite_file_path(database_url)
    if own is None:
        return None
    for uri in mlflow_uris:
        other = sqlite_file_path(uri)
        if other is not None and other == own:
            return own
    return None
