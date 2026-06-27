"""knowledge graph config and Apache AGE retrieval

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-12

Adds per-version graph configuration so GraphRAG builds are reproducible and
comparable across rollbacks, plus the metadata shape needed for optional
Apache AGE sync and retrieval.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_knowledge_base_versions",
        sa.Column("graph_config", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("caliber_knowledge_base_versions", "graph_config")
