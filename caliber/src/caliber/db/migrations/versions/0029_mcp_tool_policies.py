"""mcp tool policies

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-07

Adds per-tool policy storage on ``caliber_mcp_servers``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_mcp_servers") as batch_op:
        batch_op.add_column(
            sa.Column("tool_policies", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_mcp_servers") as batch_op:
        batch_op.drop_column("tool_policies")

