"""add optional project_id to caliber_personal_access_tokens (PAT project binding)

`P1-E` (docs/workspace-plan.md Phase 1 item 7): a nullable project-binding
column plus an index for "which PATs are bound to project X" lookups. NULL
means "not project-bound" -- unchanged behavior, bounded only by the owner's
live global scopes and workspace memberships, exactly as before this
migration. Existing rows are left NULL (the column's own default), not
backfilled from any inferred project -- inferring one would be a guess about
intent this migration has no basis for. Bare string column, no FK
constraint, matching the same "optionally project-scoped" columns already
on `CaliberWorkflow`/`CaliberEvalDataset`/`CaliberReviewQueue`.

Revision ID: 0094
Revises: 0093
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094"
down_revision: str | Sequence[str] | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_personal_access_tokens",
        sa.Column("project_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_pat_project", "caliber_personal_access_tokens", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_pat_project", table_name="caliber_personal_access_tokens")
    op.drop_column("caliber_personal_access_tokens", "project_id")
