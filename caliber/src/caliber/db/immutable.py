"""Records that are written once and never changed (B02.1a, #656).

The database is the authority: a trigger on each immutable table refuses UPDATE and DELETE (and, on PostgreSQL,
TRUNCATE), and the runtime role is granted no UPDATE/DELETE/TRUNCATE on it. This module is the *application*
layer on top, so a mistake fails early with a name instead of as a driver error at flush time:

* :func:`guard_immutable` refuses an ORM-flushed update or delete of an instance of a registered model;
* the ``Session`` ``do_orm_execute`` hook refuses an ORM-enabled bulk ``update(Model)`` / ``delete(Model)``.

Both raise :class:`ImmutableRecordError`. A Core statement against the table (``update(Model.__table__)``, raw
SQL) bypasses the ORM and is refused by the database trigger instead.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.orm.mapper import Mapper


class ImmutableRecordError(RuntimeError):
    """An attempt to change or remove a record that is immutable once written."""


#: Classes registered by :func:`guard_immutable`.
_IMMUTABLE_MODELS: set[type] = set()


def _refuse(model: type, operation: str) -> ImmutableRecordError:
    return ImmutableRecordError(f"{model.__tablename__} is immutable: {operation} refused")  # type: ignore[attr-defined]


def guard_immutable(model: type) -> type:
    """Register ``model`` as write-once; returns it so it can be used as a decorator."""
    if model in _IMMUTABLE_MODELS:
        return model
    _IMMUTABLE_MODELS.add(model)

    @event.listens_for(model, "before_update")
    def _no_update(_mapper: Mapper[Any], _connection: Any, _target: Any) -> None:
        raise _refuse(model, "UPDATE")

    @event.listens_for(model, "before_delete")
    def _no_delete(_mapper: Mapper[Any], _connection: Any, _target: Any) -> None:
        raise _refuse(model, "DELETE")

    return model


@event.listens_for(Session, "do_orm_execute")
def _no_bulk_change(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_arguments.get("mapper") if state.bind_arguments else None
    model = getattr(mapper, "class_", None)
    if model in _IMMUTABLE_MODELS:
        raise _refuse(model, "UPDATE" if state.is_update else "DELETE")


__all__ = ["ImmutableRecordError", "guard_immutable"]
