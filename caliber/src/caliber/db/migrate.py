"""``caliber-migrate``: bring the CALIBER database to the Alembic head.

``create_app`` never migrates, and a pip-installed wheel ships neither ``alembic.ini``
nor a working directory for the ``alembic`` command, which resolves its scripts from a
relative path inside the source checkout. A first native install therefore started with
no tables and failed every login with ``no such table: caliber_user_accounts``
(see ``docs/caliber-alpha-gaps.md``, P0-5).

This command builds the Alembic configuration from the installed package instead, so
it works from any directory:

    CALIBER_DATABASE_URL=sqlite:///caliber.db caliber-migrate

The database URL comes from ``CALIBER_DATABASE_URL``, exactly as the running server and
``migrations/env.py`` read it. The URL is never printed because PostgreSQL URLs
commonly embed a password.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.exc import DBAPIError

#: Alembic resolves ``package:path`` through ``importlib.resources``, so this works from
#: an installed wheel and from a source checkout alike.
SCRIPT_LOCATION = "caliber:db/migrations"


#: CALIBER's own Alembic version table. MLflow runs migrations against the same backend
#: store and uses the default ``alembic_version`` table, so the two histories coexist only
#: because CALIBER's is namespaced. ``migrations/env.py`` imports this name.
VERSION_TABLE = "caliber_alembic_version"


_VERSION_TABLE = Table(VERSION_TABLE, MetaData(), Column("version_num", String(32)))


def build_config() -> Config:
    """An Alembic ``Config`` that needs no ``alembic.ini`` and no working directory."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


@cache
def _script_directory() -> ScriptDirectory:
    """The packaged migration scripts. Immutable while the process runs, so cached."""
    return ScriptDirectory.from_config(build_config())


def head_revision() -> str | None:
    """The newest migration revision shipped in this installed package."""
    return _script_directory().get_current_head()


@dataclass(frozen=True)
class SchemaStatus:
    """Where a database stands relative to the migrations this package ships.

    ``state`` is one of:

    ``current``
        the database is at the packaged head.
    ``behind``
        a known, older revision; ``pending`` migrations remain.
    ``unknown``
        a revision this package does not ship (usually a newer release migrated it).
    ``uninitialised``
        the version table is missing or empty: ``caliber-migrate`` has never run.
    """

    state: str
    current: str | None
    head: str | None
    pending: int = 0

    @property
    def ok(self) -> bool:
        return self.state == "current"

    def describe(self) -> str:
        """One sentence for a readiness detail or log line; never includes a URL."""
        if self.state == "current":
            return f"schema at head {self.head}"
        if self.state == "behind":
            return (
                f"schema is behind: database is at {self.current}, this release expects "
                f"{self.head} ({self.pending} migration(s) pending); run caliber-migrate"
            )
        if self.state == "unknown":
            return (
                f"database revision {self.current} is not known to this release (head "
                f"{self.head}); it was probably migrated by a newer version"
            )
        return (
            f"schema is not initialised ({VERSION_TABLE} is missing or empty); run caliber-migrate"
        )


def schema_status(session: Any) -> SchemaStatus:
    """Compare the database behind ``session`` with the packaged migration head."""
    head = head_revision()
    try:
        current = session.execute(select(_VERSION_TABLE.c.version_num)).scalar()
    except DBAPIError:
        return SchemaStatus("uninitialised", None, head)
    if current is None:
        return SchemaStatus("uninitialised", None, head)
    current = str(current)
    if current == head:
        return SchemaStatus("current", current, head)
    script = _script_directory()
    try:
        script.get_revision(current)
        # ``iterate_revisions(upper, lower)`` yields the revisions above ``lower``.
        pending = len(list(script.iterate_revisions(head, current)))
    except Exception:  # alembic raises CommandError/ResolutionError for an unknown id
        return SchemaStatus("unknown", current, head)
    return SchemaStatus("behind", current, head, pending)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="caliber-migrate",
        description="Apply CALIBER database migrations (alembic upgrade) to CALIBER_DATABASE_URL.",
    )
    parser.add_argument(
        "revision",
        nargs="?",
        default="head",
        help="target revision (default: head)",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="print the database's current revision instead of migrating",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("CALIBER_DATABASE_URL", "").strip():
        sys.stderr.write(
            "caliber-migrate: CALIBER_DATABASE_URL is not set. Point it at the CALIBER "
            "database, for example sqlite:///caliber.db\n"
        )
        return 2

    config = build_config()
    if args.current:
        command.current(config, verbose=False)
        return 0
    command.upgrade(config, args.revision)
    sys.stdout.write(f"caliber-migrate: database is at {args.revision}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
