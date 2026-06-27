"""drop conversation_policies

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-15

The conversation-policies feature was removed (it was lightly used and not
load-bearing in the refinement loop). Drop its table so the migration-built
schema stays in sync with the ORM models (``CaliberConversationPolicy`` deleted).
The downgrade recreates the table as it stood after 0012 + 0026 (project
scoping) so the migration history remains reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("caliber_conversation_policies")


def downgrade() -> None:
    op.create_table(
        "caliber_conversation_policies",
        sa.Column("policy_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("owner", sa.String(length=256), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column(
            "visibility", sa.String(length=16), nullable=False, server_default="project"
        ),
        sa.UniqueConstraint("name", name="uq_conversation_policy_name"),
    )
