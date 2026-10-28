"""bind a runtime approval request to the exact effect proposal it approves

`B06.1` (#706, ADR 0003 ``approval_request``): ``caliber_runtime_approval_requests`` gains the columns that tie a request to
one effect proposal and to what the approver was shown: ``proposal_id`` (a foreign key to ``caliber_effect_proposals``),
``intent_hash`` (the proposal's, ``sha256:`` and 64 hex digits, so 71 characters like the proposal's own), ``evidence_hash``,
``policy_digest``, ``expected_resource_version`` (256, like the proposal's), ``expires_at``, ``requested_by``, and the two
lifecycle markers the later steps set, ``superseded_by`` (B06.2) and ``consumed_at`` (B06.4). Every column is nullable: a row
that already exists is a node-level approval that guards no proposal and keeps ``NULL`` in all of them (it stays decidable
exactly as before). The binding is enforced by the database, not left to the writer:

* ``ck_runtime_approval_bound``: a row is either legacy (every binding column ``NULL``) or fully bound (``proposal_id``,
  ``intent_hash``, ``evidence_hash``, ``policy_digest``, ``expires_at`` and ``requested_by`` all present), never half-bound in
  either direction; ``ck_runtime_approval_digest_length``: the three hash and digest columns are exactly 71 characters.
* a BEFORE UPDATE trigger (SQLite and PostgreSQL, the pattern of migration 0123) refuses any change to ``proposal_id``,
  ``intent_hash``, ``evidence_hash``, ``policy_digest``, ``expected_resource_version``, ``expires_at`` and ``requested_by``,
  so a request cannot be re-bound to another intent, nor a legacy row bound after the fact. ``status``, the decision columns,
  ``policy_snapshot`` (a quorum accumulates in it), ``superseded_by`` and ``consumed_at`` still move.

No backfill; the decision path refuses a bound request until B06.2 and B06.3 (readers before writers). Downgrade drops the
trigger, the checks, the index and the columns; a bound request is lost, a legacy one is untouched.

Revision ID: 0127
Revises: 0126
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0127"
down_revision: str | Sequence[str] | None = "0126"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_runtime_approval_requests"
_INDEX = "ix_runtime_approvals_proposal"
_FK = "fk_runtime_approval_proposal"
_CHECK_BOUND = "ck_runtime_approval_bound"
_CHECK_LENGTH = "ck_runtime_approval_digest_length"
_BOUND = (
    "(proposal_id IS NULL AND intent_hash IS NULL AND evidence_hash IS NULL AND policy_digest IS NULL "
    "AND expected_resource_version IS NULL AND expires_at IS NULL AND requested_by IS NULL) "
    "OR (proposal_id IS NOT NULL AND intent_hash IS NOT NULL AND evidence_hash IS NOT NULL "
    "AND policy_digest IS NOT NULL AND expires_at IS NOT NULL AND requested_by IS NOT NULL)"
)
_LENGTH = (
    "(intent_hash IS NULL OR length(intent_hash) = 71) "
    "AND (evidence_hash IS NULL OR length(evidence_hash) = 71) "
    "AND (policy_digest IS NULL OR length(policy_digest) = 71)"
)
_FROZEN = (
    "proposal_id",
    "intent_hash",
    "evidence_hash",
    "policy_digest",
    "expected_resource_version",
    "expires_at",
    "requested_by",
)
_COLUMNS = (*_FROZEN, "superseded_by", "consumed_at")
_TRIGGER = f"trg_{_TABLE}_frozen"
_FUNCTION = "caliber_runtime_approvals_frozen"
_FROZEN_MESSAGE = "the binding of a runtime approval request is frozen"


def _pg_function(schema: str) -> str:
    changed = " OR ".join(f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text" for c in _FROZEN)
    return f"""
CREATE FUNCTION "{schema}".{_FUNCTION}() RETURNS trigger LANGUAGE plpgsql SET search_path = "{schema}", pg_temp AS $$
BEGIN
    IF {changed} THEN
        RAISE EXCEPTION '{_FROZEN_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$"""


def _sqlite_trigger() -> str:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _FROZEN)
    return (
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON {_TABLE} "
        f"WHEN {changed} BEGIN SELECT RAISE(ABORT, '{_FROZEN_MESSAGE}'); END"
    )


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # SQLite rebuilds the table for a batch operation, which drops its triggers: the trigger is created last.
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("proposal_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("intent_hash", sa.String(length=71), nullable=True))
        batch_op.add_column(sa.Column("evidence_hash", sa.String(length=71), nullable=True))
        batch_op.add_column(sa.Column("policy_digest", sa.String(length=71), nullable=True))
        batch_op.add_column(
            sa.Column("expected_resource_version", sa.String(length=256), nullable=True)
        )
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("superseded_by", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("consumed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("requested_by", sa.String(length=256), nullable=True))
        batch_op.create_foreign_key(
            _FK, "caliber_effect_proposals", ["proposal_id"], ["proposal_id"]
        )
        batch_op.create_check_constraint(_CHECK_BOUND, _BOUND)
        batch_op.create_check_constraint(_CHECK_LENGTH, _LENGTH)
        batch_op.create_index(_INDEX, ["proposal_id"])
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(sa.text(_pg_function(schema)))
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON {_TABLE} "
                f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
            )
        )
    else:
        op.execute(sa.text(_sqlite_trigger()))


def downgrade() -> None:
    if _is_postgresql():
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_TABLE}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION}()"))
    else:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER}"))
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.drop_constraint(_CHECK_LENGTH, type_="check")
        batch_op.drop_constraint(_CHECK_BOUND, type_="check")
        batch_op.drop_constraint(_FK, type_="foreignkey")
        for column in reversed(_COLUMNS):
            batch_op.drop_column(column)
