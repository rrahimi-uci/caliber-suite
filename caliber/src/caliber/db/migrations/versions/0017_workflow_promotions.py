"""workflow promotion requests

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-30

Adds ``caliber_workflow_promotions`` (plan §15.3): promoting to a gated alias
(``prod``) records a pending promotion request after deploy gates pass, instead
of rotating the alias directly. A reviewer approves the request to rotate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workflow_promotions",
        sa.Column("promotion_id", sa.String(64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(128),
            sa.ForeignKey("caliber_workflows.workflow_id"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column(
            "version_id",
            sa.String(64),
            sa.ForeignKey("caliber_workflow_versions.version_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("gate_result", sa.JSON, nullable=True),
        sa.Column("requested_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("decided_by", sa.String(256), nullable=True),
        sa.Column("decided_at", sa.DateTime, nullable=True),
        sa.Column("decision_reason", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_workflow_promotions_pending",
        "caliber_workflow_promotions",
        ["workflow_id", "alias", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_promotions_pending", table_name="caliber_workflow_promotions")
    op.drop_table("caliber_workflow_promotions")
