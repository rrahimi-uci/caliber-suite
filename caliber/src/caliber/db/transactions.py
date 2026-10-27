"""Transaction helpers shared by the writers that need a real SAVEPOINT (development SQLite only)."""

from __future__ import annotations

from sqlalchemy.orm import Session


def begin_sqlite_transaction(session: Session) -> None:
    """Make SQLite's transaction start now, so a savepoint is not the first statement and cannot autocommit.

    pysqlite (legacy transaction control) begins a transaction only at the first INSERT/UPDATE/DELETE; a SAVEPOINT
    issued before that, and its RELEASE, would commit the claim on the spot and a rollback of the request would
    leave the owner behind. Development only; PostgreSQL begins a transaction with the first statement.
    """
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return
    raw = session.connection().connection.driver_connection
    if raw is not None and not raw.in_transaction:
        raw.execute("BEGIN")


__all__ = ["begin_sqlite_transaction"]
