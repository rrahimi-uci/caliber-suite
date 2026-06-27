"""add manifest_snapshot to workflow runs

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-13

Queued editor runs can now capture an unsaved workflow draft as an immutable
per-run manifest snapshot. Persist it outside ``summary`` so run lists and
polling responses stay compact while workers, replay, and debugger surfaces can
still execute and inspect the exact draft that was launched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_workflow_runs",
        sa.Column("manifest_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("caliber_workflow_runs") as batch_op:
        batch_op.drop_column("manifest_snapshot")
