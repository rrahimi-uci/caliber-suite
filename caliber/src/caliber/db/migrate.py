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

from alembic import command
from alembic.config import Config

#: Alembic resolves ``package:path`` through ``importlib.resources``, so this works from
#: an installed wheel and from a source checkout alike.
SCRIPT_LOCATION = "caliber:db/migrations"


def build_config() -> Config:
    """An Alembic ``Config`` that needs no ``alembic.ini`` and no working directory."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


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
