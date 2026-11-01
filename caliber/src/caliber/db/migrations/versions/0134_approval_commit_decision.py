"""persist the commit-time policy decision id on the approval request the dispatcher consumed

`B11.3c` (#1061): the dispatcher's commit check (``approval_valid_at_commit``) records one run policy decision and
the ``effect.dispatch`` span names it, but nothing stored *which effect* that decision authorised: the proposal keeps
only the approval id, and its authority-bearing columns are frozen (migrations 0123 and 0124), so the causal-links API
could only call the link ``unrecorded``. ``caliber_runtime_approval_requests`` gains the nullable ``commit_decision_id``
(64, like ``caliber_policy_decisions.decision_id``). The dispatcher writes it in the claim transaction, in the same
UPDATE that sets ``consumed_at``, from the ``ApprovalCommit`` the validator returned, so a refused or rolled-back dispatch
leaves nothing. The approval request, not the receipt, carries it: consumption is the one event the decision belongs to,
the row is the one the dispatcher already locks and consumes, and a receipt is written per attempt after the connector was
called (a retry would need a second copy of a fact that was fixed before the first attempt).

The database enforces that it is written once and only by consumption, with a BEFORE INSERT and a BEFORE UPDATE trigger
(SQLite and PostgreSQL; the functions are pinned to the migration's schema with ``pg_temp`` last in ``search_path``):

* an INSERT may not carry it;
* an UPDATE may set it only from ``NULL``, to a non-empty value, on a bound request (``proposal_id`` set) whose
  ``consumed_at`` moves from ``NULL`` to a value in that same UPDATE, so raw SQL cannot attach a decision to an
  unconsumed or already consumed (legacy) row, nor to a node-level approval that guards no proposal;
* once set, neither it nor ``consumed_at`` can change.

The proposal's frozen columns and the migration-0127 binding trigger are untouched (the new triggers are separate and
additive; ``ADD COLUMN`` does not rebuild the table, so SQLite keeps the 0127 trigger). There is deliberately no foreign
key, as for the decision row itself: the reader resolves the id by run and calls a vanished row a broken chain.

No backfill. A request consumed before this revision keeps ``NULL`` and the causal-links API keeps calling its decision
link ``unrecorded``: the pairing is never inferred from timestamps. The column is read only by the causal-links API and
written only by the dispatcher; migrations run before the new code serves, as for every column added to a hot table.
Downgrade drops the triggers, the functions and the column; the recorded decision ids are lost, the decisions themselves
(immutable rows) are not.

Revision ID: 0134
Revises: 0133
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0134"
down_revision: str | Sequence[str] | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_runtime_approval_requests"
_COLUMN = "commit_decision_id"
_INSERT_TRIGGER = f"trg_{_TABLE}_commit_decision_insert"
_UPDATE_TRIGGER = f"trg_{_TABLE}_commit_decision_once"
_INSERT_FUNCTION = "caliber_runtime_approvals_commit_decision_insert"
_UPDATE_FUNCTION = "caliber_runtime_approvals_commit_decision_once"
_INSERT_MESSAGE = "the commit decision of a runtime approval request is set only by consumption"
_ONCE_MESSAGE = "the commit decision of a runtime approval request is set once, by consumption"

#: The one legal UPDATE of the column: NULL to a value, on a bound request, with ``consumed_at`` set in the same UPDATE.
_CONSUMING = (
    f"OLD.{_COLUMN} IS NULL AND NEW.{_COLUMN} IS NOT NULL AND length(NEW.{_COLUMN}) > 0 "
    "AND NEW.proposal_id IS NOT NULL AND OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL"
)


def _pg_insert_function(schema: str) -> str:
    return f"""
CREATE FUNCTION "{schema}".{_INSERT_FUNCTION}() RETURNS trigger LANGUAGE plpgsql SET search_path = "{schema}", pg_temp AS $$
BEGIN
    IF NEW.{_COLUMN} IS NOT NULL THEN
        RAISE EXCEPTION '{_INSERT_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$"""


def _pg_update_function(schema: str) -> str:
    return f"""
CREATE FUNCTION "{schema}".{_UPDATE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql SET search_path = "{schema}", pg_temp AS $$
BEGIN
    IF NEW.{_COLUMN} IS DISTINCT FROM OLD.{_COLUMN} THEN
        IF NOT ({_CONSUMING}) THEN
            RAISE EXCEPTION '{_ONCE_MESSAGE}' USING ERRCODE = '23000';
        END IF;
    ELSIF OLD.{_COLUMN} IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
        RAISE EXCEPTION '{_ONCE_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$"""


def _sqlite_triggers() -> tuple[str, str]:
    return (
        f"CREATE TRIGGER {_INSERT_TRIGGER} BEFORE INSERT ON {_TABLE} "
        f"WHEN NEW.{_COLUMN} IS NOT NULL BEGIN SELECT RAISE(ABORT, '{_INSERT_MESSAGE}'); END",
        f"CREATE TRIGGER {_UPDATE_TRIGGER} BEFORE UPDATE ON {_TABLE} "
        f"WHEN (NEW.{_COLUMN} IS NOT OLD.{_COLUMN} AND NOT ({_CONSUMING})) "
        f"OR (NEW.{_COLUMN} IS OLD.{_COLUMN} AND OLD.{_COLUMN} IS NOT NULL "
        "AND NEW.consumed_at IS NOT OLD.consumed_at) "
        f"BEGIN SELECT RAISE(ABORT, '{_ONCE_MESSAGE}'); END",
    )


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # A plain ADD COLUMN (no batch rebuild): SQLite keeps the 0127 binding trigger, which a table rebuild would drop.
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(sa.text(_pg_insert_function(schema)))
        op.execute(sa.text(_pg_update_function(schema)))
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_INSERT_TRIGGER} BEFORE INSERT ON {_TABLE} "
                f"FOR EACH ROW EXECUTE FUNCTION {_INSERT_FUNCTION}()"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_UPDATE_TRIGGER} BEFORE UPDATE ON {_TABLE} "
                f"FOR EACH ROW EXECUTE FUNCTION {_UPDATE_FUNCTION}()"
            )
        )
    else:
        for statement in _sqlite_triggers():
            op.execute(sa.text(statement))


def downgrade() -> None:
    if _is_postgresql():
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER} ON {_TABLE}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_INSERT_TRIGGER} ON {_TABLE}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_UPDATE_FUNCTION}()"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_INSERT_FUNCTION}()"))
        op.drop_column(_TABLE, _COLUMN)
    else:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_INSERT_TRIGGER}"))
        # SQLite cannot drop a column a trigger names; the triggers are gone above.
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))
