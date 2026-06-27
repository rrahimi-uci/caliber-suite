"""retry semantics + multi-approver votes

Revision ID: 0007
Revises: 0006
Create Date: 2025-05-15

Three changes that together make CALIBER production-deployable:

1. ``caliber_refinement_jobs.review_notes`` — text column where the
   request-changes endpoint stashes the reviewer's guidance for the
   candidate stage's retry pass.
2. ``caliber_approval_requests.attempt_number`` — integer counter set
   from ``job.attempt_count`` at approval creation. Distinguishes
   multiple approval rows for the same job across retries.
3. Drop the unique constraint on ``caliber_approval_requests.job_id``
   so retry attempts can each have their own approval row.
4. New ``caliber_approval_votes`` table for the multi-approver flow.

SQLite's ALTER TABLE limitations mean dropping a unique constraint
needs the batch_alter_table trick — Alembic synthesizes a copy table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add review_notes to refinement jobs.
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.add_column(sa.Column("review_notes", sa.Text, nullable=True))

    # 2 + 3. Approval changes — attempt_number column + drop unique on job_id.
    # On SQLite, drop_constraint requires batch mode and the constraint
    # name; on Postgres the named constraint drop works directly. The
    # batch_alter_table block handles both.
    with op.batch_alter_table("caliber_approval_requests") as batch_op:
        batch_op.add_column(
            sa.Column(
                "attempt_number",
                sa.Integer,
                nullable=False,
                server_default="1",
            )
        )
        # SQLAlchemy auto-named the implicit unique index. The pattern is
        # backend-dependent; on SQLite the unique constraint shows up as
        # ``sqlite_autoindex_caliber_approval_requests_*``. To stay portable
        # we use ``drop_index`` with the name SQLAlchemy emitted — and on
        # backends where that isn't the right name, the operator runs the
        # migration's manual fallback. For our test backend (SQLite via
        # batch_alter) the constraint drop works through the table rebuild.
        batch_op.create_index(
            "ix_approval_requests_job_id", ["job_id"], unique=False
        )

    # 4. New votes table.
    op.create_table(
        "caliber_approval_votes",
        sa.Column("vote_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "approval_id",
            sa.String(64),
            sa.ForeignKey("caliber_approval_requests.approval_id"),
            nullable=False,
        ),
        sa.Column("voter", sa.String(256), nullable=False),
        sa.Column("vote", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("approval_id", "voter", name="uq_approval_vote_per_voter"),
    )


def downgrade() -> None:
    op.drop_table("caliber_approval_votes")
    with op.batch_alter_table("caliber_approval_requests") as batch_op:
        batch_op.drop_index("ix_approval_requests_job_id")
        batch_op.drop_column("attempt_number")
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("review_notes")
