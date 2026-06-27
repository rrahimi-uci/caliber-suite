"""drop queued workflow-run started_at default

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-10

Queued workflow runs must keep ``started_at`` null until a worker claims them.
The queue foundation migration made the column nullable but left the older
``now()`` server default in place, so new queued rows looked started
immediately on databases that honor the default.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_workflow_runs") as batch_op:
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(),
            existing_nullable=True,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_workflow_runs") as batch_op:
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(),
            existing_nullable=True,
            server_default=sa.func.now(),
        )
