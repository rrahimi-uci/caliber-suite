"""runtime locks

Revision ID: 0003
Revises: 0002
Create Date: 2025-05-15

Adds ``caliber_runtime_locks``. Used by the feedback poller (and any future
singleton background task) to store its checkpoint and lease.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_runtime_locks",
        sa.Column("lock_name", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("checkpoint", sa.JSON, nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("caliber_runtime_locks")
