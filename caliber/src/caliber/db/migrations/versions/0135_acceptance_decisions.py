"""persist acceptance decisions, immutable and keyed by run, candidate, criteria version and observation digest

`B09.2a` (#784): ``caliber_acceptance_decisions`` holds one row per acceptance decision, the persisted form of the
Harborlight predicates' verdict (``caliber.acceptance``, B09.1, #779). The row is unique on ``(workflow_run_id,
candidate_id, criteria_version, observation_digest)``: recording the same decision again finds the row, a new candidate,
criteria version or observation is a new row. ``criteria`` is the per-criterion evidence (predicate id, verdict, the JSON
pointer into the observations that decided it, the rule ids) and ``criteria_total`` / ``criteria_failed`` count it.

The table refuses, with CHECK constraints, what the code never writes: an ``accepted`` row with a failed criterion (every
predicate is critical, and a successful transport is not an input) or with a reason, a row that has neither criteria nor a
reason, a ``reason_code`` other than ``invalid_observations`` (a rejection recorded because the observations could not be
frozen; it carries no criteria), counts out of range, and a digest that is not 64 characters. The record is also written
once: a BEFORE UPDATE/DELETE trigger refuses any change (SQLite and PostgreSQL; PostgreSQL adds a BEFORE TRUNCATE trigger and
revokes ``UPDATE``, ``DELETE`` and ``TRUNCATE`` from ``PUBLIC``; the trigger function is pinned to the migration's schema with
``pg_temp`` last in ``search_path``). The foreign key to the run is enforced on PostgreSQL (SQLite leaves foreign keys
unenforced, so the writer checks the run itself).

The migration is purely additive (a new table nothing else references) and nothing reads it before the code that writes it,
so there is no rolling-deploy hazard in either direction. A process that runs the new code before this migration has no table to
record in: the recording call fails and nothing is accepted; nothing in the runtime calls it yet (B09.2b, #791). Downgrade drops
the table and the function and loses the decisions; it never runs where they matter.

Revision ID: 0135
Revises: 0134
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0135"
down_revision: str | Sequence[str] | None = "0134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_acceptance_decisions"
_FUNCTION = "caliber_acceptance_decisions_immutable"
_MESSAGE = "caliber_acceptance_decisions is immutable"


def _pg_function(schema: str) -> str:
    return f"""
CREATE FUNCTION "{schema}".{_FUNCTION}() RETURNS trigger LANGUAGE plpgsql
    SET search_path = "{schema}", pg_temp AS $$
BEGIN
    RAISE EXCEPTION '{_MESSAGE}: % refused', TG_OP USING ERRCODE = '23000';
END;
$$"""


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()",
    f"CREATE TRIGGER trg_{_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
    f"FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION}()",
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
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("criteria_version", sa.String(length=64), nullable=False),
        sa.Column("observation_digest", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("criteria_total", sa.Integer(), nullable=False),
        sa.Column("criteria_failed", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("reason_detail", sa.String(length=512), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workflow_run_id",
            "candidate_id",
            "criteria_version",
            "observation_digest",
            name="uq_acceptance_decision_key",
        ),
        sa.CheckConstraint(
            "length(candidate_id) > 0 AND length(criteria_version) > 0",
            name="ck_acceptance_decision_identity",
        ),
        sa.CheckConstraint("length(observation_digest) = 64", name="ck_acceptance_decision_digest"),
        sa.CheckConstraint(
            "criteria_total >= 0 AND criteria_failed >= 0 AND criteria_failed <= criteria_total",
            name="ck_acceptance_decision_counts",
        ),
        sa.CheckConstraint(
            "(reason_code IS NULL AND reason_detail IS NULL AND criteria_total > 0) OR "
            "(reason_code = 'invalid_observations' AND reason_detail IS NOT NULL "
            "AND length(reason_detail) > 0 AND criteria_total = 0 AND outcome = 'rejected')",
            name="ck_acceptance_decision_reason",
        ),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND criteria_failed = 0 AND reason_code IS NULL) OR "
            "(outcome = 'rejected' AND (criteria_failed > 0 OR reason_code IS NOT NULL))",
            name="ck_acceptance_decision_critical",
        ),
    )
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
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION}()"))
