"""add durable release operations

Revision ID: 0082
Revises: 0081
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | Sequence[str] | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_release_operations",
        sa.Column("operation_id", sa.String(64), primary_key=True),
        sa.Column("operation_type", sa.String(16), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_name", sa.String(256), nullable=False),
        sa.Column("target_name", sa.String(128), nullable=False),
        sa.Column("active_lock", sa.String(512), nullable=True),
        sa.Column("version_before", sa.Integer, nullable=True),
        sa.Column("version_after", sa.Integer, nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("effective_scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("evidence", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("approval_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("provider_result", sa.JSON, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("applied_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("active_lock", name="uq_release_operations_active_lock"),
    )
    op.create_index(
        "ix_release_operations_status_created",
        "caliber_release_operations",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_release_operations_resource_target",
        "caliber_release_operations",
        ["resource_type", "resource_name", "target_name"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_release_operations_resource_target", table_name="caliber_release_operations"
    )
    op.drop_index(
        "ix_release_operations_status_created", table_name="caliber_release_operations"
    )
    op.drop_table("caliber_release_operations")
