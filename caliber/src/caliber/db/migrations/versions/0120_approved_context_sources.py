"""add the approved context source inventory

`B07.1a` (#692): the inventory of knowledge-base versions a project has approved as sources of run context
(``caliber_approved_context_sources``, unique ``(project_id, knowledge_base_id, version_id)``). The table starts empty
and nothing reads it until the admission and load checks of B07.1b and B07.2 arrive. Downgrade drops it and with it
the approvals.

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0120"
down_revision: str | Sequence[str] | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_approved_context_sources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_base_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=256), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["caliber_projects.project_id"]),
        sa.UniqueConstraint(
            "project_id", "knowledge_base_id", "version_id", name="uq_approved_context_source"
        ),
    )
    op.create_index(
        "ix_approved_context_sources_project",
        "caliber_approved_context_sources",
        ["project_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_approved_context_sources_project", table_name="caliber_approved_context_sources")
    op.drop_table("caliber_approved_context_sources")
