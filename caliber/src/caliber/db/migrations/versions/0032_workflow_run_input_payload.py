"""add input_payload to workflow runs

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-12

Queued (async) workflow runs previously stored only a 1000-char preview of the
run input in ``summary['input']``, which is also what the worker replayed from —
so async runs silently truncated inputs longer than 1000 chars (a whole-document
extraction would see only its first ~150 words). This adds an untruncated
``input_payload`` TEXT column the worker reads for execution. It is intentionally
NOT part of ``WorkflowRunSchema``, so polling responses stay small. Nullable so
existing rows + the sync path are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_workflow_runs",
        sa.Column("input_payload", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("caliber_workflow_runs") as batch_op:
        batch_op.drop_column("input_payload")
