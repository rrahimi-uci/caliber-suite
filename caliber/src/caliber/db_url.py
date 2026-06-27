"""Database URL normalization helpers."""

from __future__ import annotations


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
