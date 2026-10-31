"""persist the per-step token reservations of governed agent steps

`B08.4` (#761): ``caliber_step_token_reservations`` holds one row per ``(workflow_run_id, node_id, occurrence_id,
attempt)`` (unique), the tokens a governed agent step was admitted to spend, reserved before its adapter is called under
the run's row lock so concurrent workers cannot together reserve more than the run's budget. A row is ``reserved`` (open)
until it is moved exactly once to ``settled`` (the step returned) or ``abandoned`` (the owner janitor found it open past the
step's deadline plus a grace); the charge is then ``charged_tokens`` on a ``reported`` basis (the usage a provider-sourced
result reported) or a ``conservative`` one (the whole reservation, never below it). CHECK constraints keep every figure
non-negative and the charge consistent with its basis. The database also enforces that a final row is final: a BEFORE UPDATE
trigger (SQLite and PostgreSQL) refuses any UPDATE of a ``settled`` or ``abandoned`` row and any change to the identity and
the reserved figures of an open one, a BEFORE DELETE trigger refuses deletion, and PostgreSQL adds a BEFORE TRUNCATE trigger
and revokes DELETE and TRUNCATE from PUBLIC (the trigger functions are pinned to the migration's schema with ``pg_temp`` last
in ``search_path``). On SQLite (the development dialect) the triggers refuse UPDATE and DELETE statements; ``INSERT OR
REPLACE`` and unenforced foreign keys can still be used from raw SQL.

The migration is purely additive (a new table nothing else references) and nothing reads it before the code that writes it,
so there is no rolling-deploy hazard in the other direction either. A process that runs the new code before this migration
has no table to reserve in: a governed step refuses (``store_failed``) and never calls its adapter. Downgrade drops the table
and the functions and loses the reservations (the charges that were counted against runs); it never runs where they matter.

Revision ID: 0133
Revises: 0132
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0133"
down_revision: str | Sequence[str] | None = "0132"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_step_token_reservations"
#: What an open row's own UPDATE may never change: the identity and the figures it was admitted under.
_FROZEN = (
    "reservation_id",
    "workflow_run_id",
    "node_id",
    "occurrence_id",
    "attempt",
    "step_request_id",
    "reserved_tokens",
    "budget_tokens",
    "deadline_at",
    "created_at",
)
_FINAL_MESSAGE = "a settled or abandoned step token reservation is final"
_FROZEN_MESSAGE = "the identity and reserved figures of a step token reservation are immutable"
_DELETE_MESSAGE = "caliber_step_token_reservations is immutable: DELETE refused"


def _pg_functions(schema: str) -> tuple[str, ...]:
    pin = f'SET search_path = "{schema}", pg_temp'
    changed = " OR ".join(f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text" for c in _FROZEN)
    return (
        f"""
CREATE FUNCTION "{schema}".caliber_step_token_reservations_guard() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF OLD.state <> 'reserved' THEN
        RAISE EXCEPTION '{_FINAL_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    IF {changed} THEN
        RAISE EXCEPTION '{_FROZEN_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$""",
        f"""
CREATE FUNCTION "{schema}".caliber_step_token_reservations_no_delete() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    RAISE EXCEPTION '{_DELETE_MESSAGE.replace("DELETE", "%")}', TG_OP USING ERRCODE = '23000';
END;
$$""",
    )


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_guard BEFORE UPDATE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_step_token_reservations_guard()",
    f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_step_token_reservations_no_delete()",
    f"CREATE TRIGGER trg_{_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION caliber_step_token_reservations_no_delete()",
)


def _sqlite_triggers() -> tuple[str, ...]:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _FROZEN)
    return (
        f"CREATE TRIGGER trg_{_TABLE}_final BEFORE UPDATE ON {_TABLE} "
        f"WHEN OLD.state <> 'reserved' BEGIN SELECT RAISE(ABORT, '{_FINAL_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_frozen BEFORE UPDATE ON {_TABLE} "
        f"WHEN {changed} BEGIN SELECT RAISE(ABORT, '{_FROZEN_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
        f"BEGIN SELECT RAISE(ABORT, '{_DELETE_MESSAGE}'); END",
    )


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("reservation_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("occurrence_id", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("step_request_id", sa.String(length=128), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("budget_tokens", sa.BigInteger(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="reserved"),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=True),
        sa.Column("charge_basis", sa.String(length=16), nullable=True),
        sa.Column("reported_tokens", sa.BigInteger(), nullable=True),
        sa.Column("usage_source", sa.String(length=16), nullable=True),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "workflow_run_id",
            "node_id",
            "occurrence_id",
            "attempt",
            name="uq_step_token_reservation_step",
        ),
        sa.CheckConstraint("reserved_tokens > 0", name="ck_step_token_reservation_reserved"),
        sa.CheckConstraint("budget_tokens > 0", name="ck_step_token_reservation_budget"),
        sa.CheckConstraint("attempt >= 1", name="ck_step_token_reservation_attempt"),
        sa.CheckConstraint(
            "state IN ('reserved', 'settled', 'abandoned')", name="ck_step_token_reservation_state"
        ),
        sa.CheckConstraint(
            "charge_basis IS NULL OR charge_basis IN ('reported', 'conservative')",
            name="ck_step_token_reservation_basis",
        ),
        sa.CheckConstraint(
            "(charged_tokens IS NULL OR charged_tokens >= 0) "
            "AND (reported_tokens IS NULL OR reported_tokens >= 0)",
            name="ck_step_token_reservation_nonnegative",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND charged_tokens IS NULL AND charge_basis IS NULL "
            "AND settled_at IS NULL) OR (state IN ('settled', 'abandoned') "
            "AND charged_tokens IS NOT NULL AND charge_basis IS NOT NULL AND settled_at IS NOT NULL)",
            name="ck_step_token_reservation_final_shape",
        ),
        sa.CheckConstraint(
            "charge_basis IS NULL OR charge_basis <> 'conservative' OR charged_tokens = reserved_tokens",
            name="ck_step_token_reservation_conservative",
        ),
        sa.CheckConstraint(
            "charge_basis IS NULL OR charge_basis <> 'reported' "
            "OR (reported_tokens IS NOT NULL AND charged_tokens = reported_tokens)",
            name="ck_step_token_reservation_reported",
        ),
        sa.CheckConstraint(
            "state <> 'abandoned' OR charge_basis = 'conservative'",
            name="ck_step_token_reservation_abandoned",
        ),
        sa.CheckConstraint(
            "length(node_id) > 0 AND length(occurrence_id) > 0 AND length(step_request_id) > 0",
            name="ck_step_token_reservation_identity",
        ),
    )
    op.create_index("ix_step_token_reservations_run", _TABLE, ["workflow_run_id", "state"])
    op.create_index("ix_step_token_reservations_open", _TABLE, ["state", "deadline_at"])
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        for statement in (*_pg_functions(schema), *_PG_TRIGGERS):
            op.execute(sa.text(statement))
        op.execute(sa.text(f"REVOKE DELETE, TRUNCATE ON {_TABLE} FROM PUBLIC"))
    else:
        for statement in _sqlite_triggers():
            op.execute(sa.text(statement))


def downgrade() -> None:
    op.drop_table(_TABLE)  # drops the table's own indexes and triggers
    if _is_postgresql():
        for function in (
            "caliber_step_token_reservations_guard",
            "caliber_step_token_reservations_no_delete",
        ):
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
