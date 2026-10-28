"""add compare-and-set versioning and the seven-state transition table to effect proposals

`B05.2` (#705, ADR 0003): ``caliber_effect_proposals`` gains ``version`` (starts at 1, advances by exactly one with every
state change) and the evidence that moves with a state: ``attempt`` and ``owner_id`` (and ``dispatched_at``) recorded when
the proposal is dispatched, ``unknown_at`` and ``settled_at``, a short ``reason`` and the ``approval_kind``/``approval_ref``
that approved it. The database enforces the table itself, so a raw UPDATE cannot do what the application refuses: a BEFORE
UPDATE trigger (SQLite and PostgreSQL) refuses any change of ``state`` that is not a legal edge or that does not advance
``version`` by one, refuses a change to those columns without a state change, and refuses every change to a settled
(``committed`` or ``failed``) proposal. The legal edges are the ones in ``caliber.effects.records.TRANSITIONS`` (a test
keeps the two lists identical); the intent columns stay frozen by migration 0123's triggers. Downgrade removes the guard and
the columns (and the evidence in them); migration 0123's triggers are untouched.

Revision ID: 0124
Revises: 0123
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0124"
down_revision: str | Sequence[str] | None = "0123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_effect_proposals"
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("approved", "failed"),
    "approved": ("dispatched", "failed"),
    "dispatched": ("committed", "failed", "unknown"),
    "unknown": ("reconciled",),
    "reconciled": ("committed", "failed"),
}
_SETTLED = ("committed", "failed")
_EVIDENCE = (
    "version",
    "attempt",
    "owner_id",
    "dispatched_at",
    "unknown_at",
    "settled_at",
    "reason",
    "approval_kind",
    "approval_ref",
)
_COLUMNS = (
    ("attempt", sa.Integer(), None),
    ("owner_id", sa.String(length=128), None),
    ("dispatched_at", sa.DateTime(), None),
    ("unknown_at", sa.DateTime(), None),
    ("settled_at", sa.DateTime(), None),
    ("reason", sa.String(length=128), None),
    ("approval_kind", sa.String(length=32), None),
    ("approval_ref", sa.String(length=128), None),
)
_SETTLED_MESSAGE = "a settled effect proposal is final"
_TOGETHER_MESSAGE = "the evidence of an effect proposal changes only with its state"
_ILLEGAL_MESSAGE = "illegal effect proposal transition"
_VERSION_MESSAGE = "an effect proposal transition advances the version by one"


def _legal_pg(old: str, new: str) -> str:
    pairs = [
        f"({old} = '{a}' AND {new} = '{b}')" for a, targets in _TRANSITIONS.items() for b in targets
    ]
    return "(" + " OR ".join(pairs) + ")"


def _settled(old: str) -> str:
    return f"{old} IN ({', '.join(repr(s) for s in _SETTLED)})"


def _pg_function(schema: str) -> str:
    pin = f'SET search_path = "{schema}", pg_temp'
    changed = " OR ".join(f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text" for c in _EVIDENCE)
    return f"""
CREATE FUNCTION "{schema}".caliber_effect_proposals_guard() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF {_settled("OLD.state")} THEN
        RAISE EXCEPTION '{_SETTLED_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
        IF {changed} THEN
            RAISE EXCEPTION '{_TOGETHER_MESSAGE}' USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT {_legal_pg("OLD.state", "NEW.state")} THEN
        RAISE EXCEPTION '{_ILLEGAL_MESSAGE}: % -> %', OLD.state, NEW.state USING ERRCODE = '23000';
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
        RAISE EXCEPTION '{_VERSION_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$"""


def _sqlite_triggers() -> tuple[str, ...]:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _EVIDENCE)
    legal = (
        "("
        + " OR ".join(
            f"(OLD.state = '{a}' AND NEW.state = '{b}')"
            for a, targets in _TRANSITIONS.items()
            for b in targets
        )
        + ")"
    )
    return (
        f"CREATE TRIGGER trg_{_TABLE}_settled BEFORE UPDATE ON {_TABLE} "
        f"WHEN {_settled('OLD.state')} BEGIN SELECT RAISE(ABORT, '{_SETTLED_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_together BEFORE UPDATE ON {_TABLE} "
        f"WHEN NOT {_settled('OLD.state')} AND NEW.state IS OLD.state AND ({changed}) "
        f"BEGIN SELECT RAISE(ABORT, '{_TOGETHER_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_illegal BEFORE UPDATE ON {_TABLE} "
        f"WHEN NOT {_settled('OLD.state')} AND NEW.state IS NOT OLD.state AND NOT {legal} "
        f"BEGIN SELECT RAISE(ABORT, '{_ILLEGAL_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_version BEFORE UPDATE ON {_TABLE} "
        f"WHEN NOT {_settled('OLD.state')} AND NEW.state IS NOT OLD.state AND NEW.version IS NOT OLD.version + 1 "
        f"BEGIN SELECT RAISE(ABORT, '{_VERSION_MESSAGE}'); END",
    )


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    for name, kind, _ in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, kind, nullable=True))
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(sa.text(_pg_function(schema)))
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{_TABLE}_guard BEFORE UPDATE ON {_TABLE} "
                "FOR EACH ROW EXECUTE FUNCTION caliber_effect_proposals_guard()"
            )
        )
    else:
        for statement in _sqlite_triggers():
            op.execute(sa.text(statement))


def downgrade() -> None:
    if _is_postgresql():
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_guard ON {_TABLE}"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS caliber_effect_proposals_guard()"))
    else:
        for suffix in ("settled", "together", "illegal", "version"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_{suffix}"))
    for name in ("version", *(name for name, _, _ in _COLUMNS)):
        if _is_postgresql():
            op.drop_column(_TABLE, name)
        else:  # alembic cannot drop a column on SQLite outside batch mode, which would rebuild the table and its triggers
            op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {name}"))
