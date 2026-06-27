"""Alembic environment.

Two responsibilities:

1. Tell Alembic where to find ``target_metadata`` (the source-of-truth schema
   it diffs against). We import :data:`caliber.db.Base.metadata` so every
   model defined in :mod:`caliber.db.models` is visible.
2. Pull the DB URL from ``CALIBER_DATABASE_URL`` rather than ``alembic.ini``,
   so production deployments configure migrations the same way they configure
   the running app.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from caliber.db import Base
from caliber.db_url import normalize_database_url

# Alembic config object provides access to alembic.ini values.
config = context.config

# Configure logging from alembic.ini if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Wire model metadata so `--autogenerate` can diff DB ↔ model definitions.
target_metadata = Base.metadata

# Caliber and MLflow share a backend store in production (see
# README), and both projects run alembic migrations against it.
# Without a namespaced version table they would both write to the
# default ``alembic_version`` row and each would refuse to start
# after the other ran. Pin caliber's version history to its own
# table so the two migration histories coexist.
CALIBER_VERSION_TABLE = "caliber_alembic_version"


def _get_database_url() -> str:
    """Resolve the DB URL with this precedence: env var > alembic.ini.

    CI and production set the env var; local developers can also use the env var.
    The fallback to alembic.ini stays empty by design (see alembic.ini) so a
    misconfigured environment fails loudly rather than writing to a surprise DB.
    """
    url = os.environ.get("CALIBER_DATABASE_URL")
    if url:
        return normalize_database_url(url)
    ini_url = config.get_main_option("sqlalchemy.url")
    if not ini_url:
        raise RuntimeError(
            "no database URL configured for migrations; "
            "set CALIBER_DATABASE_URL or alembic.ini sqlalchemy.url"
        )
    return normalize_database_url(ini_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout, no live DB.

    Useful for review and for inclusion in change-management workflows.
    """
    context.configure(
        url=_get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=CALIBER_VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations 'online' — against a live DB connection."""
    ini_section = config.get_section(config.config_ini_section, {})
    ini_section["sqlalchemy.url"] = _get_database_url()
    connectable = engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=CALIBER_VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
