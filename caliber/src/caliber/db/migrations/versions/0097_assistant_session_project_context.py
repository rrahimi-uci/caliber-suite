"""persist the assistant session project context

`P2-B` (docs/workspace-plan.md Phase 2 item 5): assistant skill resolution
already honored a request identity when one was present, but a multi-turn
session had no durable project binding. A later turn without the ambient
project header could therefore resolve skills globally. The nullable column
preserves existing personal and legacy sessions while allowing new sessions
to carry their workspace context across turns.

Revision ID: 0097
Revises: 0096
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0097"
down_revision: str | Sequence[str] | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_assistant_sessions",
        sa.Column("project_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_assistant_sessions", "project_id")
