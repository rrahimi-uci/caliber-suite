"""Add logical step and attempt linkage to context manifests.

Revision ID: 0130
Revises: 0129
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0130"
down_revision: str | Sequence[str] | None = "0129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_context_manifests",
        sa.Column("node_step_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "caliber_context_manifests",
        sa.Column("occurrence_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "caliber_context_manifests",
        sa.Column("attempt", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_context_manifests_node_step",
        "caliber_context_manifests",
        ["workflow_run_id", "node_step_id", "attempt"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_manifests_node_step",
        table_name="caliber_context_manifests",
    )
    op.drop_column("caliber_context_manifests", "attempt")
    op.drop_column("caliber_context_manifests", "occurrence_id")
    op.drop_column("caliber_context_manifests", "node_step_id")
