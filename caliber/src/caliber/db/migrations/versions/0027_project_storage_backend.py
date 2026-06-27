"""project storage backend selection

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-05

Persists the storage backend chosen for each File Directory / project so
directory writes can target local filesystem or MinIO independently of the
server's default workflow storage backend.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "storage_backend",
                sa.String(16),
                nullable=False,
                server_default="local",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_projects") as batch_op:
        batch_op.drop_column("storage_backend")
