"""SQLAlchemy 2.0 declarative base.

Kept in its own module so model definitions in :mod:`caliber.db.models` import
from here without circular references. Alembic's ``env.py`` imports
:data:`Base.metadata` for autogenerate diffs.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base. All CALIBER ORM models inherit this."""
