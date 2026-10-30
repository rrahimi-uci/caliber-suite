"""add optional creator provenance to refinement jobs

`B06.1` (#731): ``caliber_refinement_jobs.triggered_by`` records the declared
principal that caused a refinement job to be created when that creation path
has one.  The column is nullable by design: existing rows have no reliable
creator fact to backfill, and supported legacy development paths may continue
to create rows without one.  The apply boundary must not infer authorization
from a NULL value; the later separation-of-duties control decides how such a
row is reviewed or quarantined.

The migration is additive and safe for rolling deployment.  Older readers
ignore the new column and newer readers accept legacy rows with NULL.  The
downgrade removes only the optional provenance column; all other job data is
left intact.

Revision ID: 0131
Revises: 0130
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0131"
down_revision: str | Sequence[str] | None = "0130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_refinement_jobs",
        sa.Column("triggered_by", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_refinement_jobs", "triggered_by")
