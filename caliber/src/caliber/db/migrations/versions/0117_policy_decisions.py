"""persist policy decisions, immutable

`B03.3` (#671, ADR 0003): every policy decision (allow or deny) is recorded with the run it was asked about, the
grant and policy version that decided it, the reason codes and a hash of its inputs. The record is written once:
a BEFORE UPDATE/DELETE trigger refuses any change (PostgreSQL adds a BEFORE TRUNCATE trigger and revokes ``UPDATE``,
``DELETE`` and ``TRUNCATE`` from ``PUBLIC``; the trigger function is pinned to the migration's schema with
``pg_temp`` last in ``search_path``). There are no foreign keys on purpose: a decision about a grant or run that does
not exist is still evidence. Downgrade drops the table and the function and loses the decisions (never runs).

Revision ID: 0117
Revises: 0116
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0117"
down_revision: str | Sequence[str] | None = "0116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_policy_decisions"
_MESSAGE = "caliber_policy_decisions is immutable"


def _pg_function(schema: str) -> str:
    return f"""
CREATE FUNCTION "{schema}".caliber_policy_decisions_immutable() RETURNS trigger LANGUAGE plpgsql
    SET search_path = "{schema}", pg_temp AS $$
BEGIN
    RAISE EXCEPTION '{_MESSAGE}: % refused', TG_OP USING ERRCODE = '23000';
END;
$$"""


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_policy_decisions_immutable()",
    f"CREATE TRIGGER trg_{_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION caliber_policy_decisions_immutable()",
)
_SQLITE_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_no_update BEFORE UPDATE ON {_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}: UPDATE refused'); END",
    f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}: DELETE refused'); END",
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("decision_id", sa.String(length=64), primary_key=True),
        sa.Column("workflow_run_id", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.String(length=64), nullable=False),
        sa.Column("subject_user_id", sa.String(length=256), nullable=False),
        sa.Column("workload_id", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=256), nullable=False),
        sa.Column("resource", sa.String(length=256), nullable=False),
        sa.Column("purpose", sa.String(length=256), nullable=False),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("result IN ('allow', 'deny')", name="ck_policy_decision_result"),
        sa.CheckConstraint("length(input_hash) = 64", name="ck_policy_decision_input_hash"),
    )
    op.create_index("ix_policy_decisions_run", _TABLE, ["workflow_run_id", "evaluated_at"])
    op.create_index("ix_policy_decisions_grant", _TABLE, ["grant_id"])
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        for statement in (_pg_function(schema), *_PG_TRIGGERS):
            op.execute(sa.text(statement))
        op.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON {_TABLE} FROM PUBLIC"))
    else:
        for statement in _SQLITE_TRIGGERS:
            op.execute(sa.text(statement))


def downgrade() -> None:
    op.drop_table(_TABLE)  # drops the table's own triggers
    if _is_postgresql():
        op.execute(sa.text("DROP FUNCTION IF EXISTS caliber_policy_decisions_immutable()"))
