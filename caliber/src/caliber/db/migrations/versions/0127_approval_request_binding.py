"""bind a runtime approval request to the exact effect proposal it approves

`B06.1` (#706, ADR 0003 ``approval_request``): ``caliber_runtime_approval_requests`` gains the columns that tie a request to
one effect proposal and to what the approver was shown: ``proposal_id`` (a foreign key to ``caliber_effect_proposals``),
``intent_hash`` (the proposal's, ``sha256:`` and 64 hex digits, so 71 characters like the proposal's own), ``evidence_hash``,
``policy_digest``, ``expected_resource_version`` (256, like the proposal's), ``expires_at``, ``requested_by``, and the two
lifecycle markers the later steps set, ``superseded_by`` (B06.2) and ``consumed_at`` (B06.4). Every column is nullable: a row
that already exists is a node-level approval that guards no proposal and keeps ``NULL`` in all of them (it stays decidable
exactly as before). ``ck_runtime_approval_bound`` makes a bound row bound completely: a row with a ``proposal_id`` must also
carry its intent hash, evidence hash, policy digest, expiry and requester, so raw SQL cannot store a half-bound request.
No backfill and no trigger: nothing is rewritten, and the decision path does not read the new columns yet (readers before
writers). Downgrade drops the check, the index and the columns; a bound request is lost, a legacy one is untouched.

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
_CHECK = "ck_runtime_approval_bound"
_BOUND = (
    "proposal_id IS NULL OR (intent_hash IS NOT NULL AND evidence_hash IS NOT NULL "
    "AND policy_digest IS NOT NULL AND expires_at IS NOT NULL AND requested_by IS NOT NULL)"
)
_COLUMNS = (
    "proposal_id",
    "intent_hash",
    "evidence_hash",
    "policy_digest",
    "expected_resource_version",
    "expires_at",
    "superseded_by",
    "consumed_at",
    "requested_by",
)


def upgrade() -> None:
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
        batch_op.create_check_constraint(_CHECK, _BOUND)
        batch_op.create_index(_INDEX, ["proposal_id"])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.drop_constraint(_CHECK, type_="check")
        batch_op.drop_constraint(_FK, type_="foreignkey")
        for column in reversed(_COLUMNS):
            batch_op.drop_column(column)
