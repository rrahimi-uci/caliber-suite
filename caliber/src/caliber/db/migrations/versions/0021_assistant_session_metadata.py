"""assistant session metadata

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-03

Adds a JSON metadata column to assistant sessions for prompt context provenance.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_assistant_sessions",
        sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("caliber_assistant_sessions", "metadata")
