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


def begin_sqlite_write_transaction(session: Session) -> None:
    """Take SQLite's write lock now (``BEGIN IMMEDIATE``), so a read-then-write that must be exclusive is.

    pysqlite runs a SELECT outside any transaction, so two threads can read the same total before either writes: a
    check-then-insert (a budget, a uniqueness the schema cannot state) is not serialized by "SQLite serializes writers".
    ``BEGIN IMMEDIATE`` makes the second writer wait (the driver's busy timeout) until the first commits. Development
    only; PostgreSQL serializes such a section with a row lock (``FOR UPDATE``), which is a no-op here. A transaction
    that is already open is left as it is.
    """
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return
    raw = session.connection().connection.driver_connection
    if raw is not None and not raw.in_transaction:
        raw.execute("BEGIN IMMEDIATE")


__all__ = ["begin_sqlite_transaction", "begin_sqlite_write_transaction"]
