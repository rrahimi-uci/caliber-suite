"""Eval run subject_ref (artifact-target scoring)

Revision ID: 0059
Revises: 0058
Create Date: 2026-06-24

Adds ``subject_ref`` to ``caliber_eval_runs`` so a run can record *what real
artifact* it scored — a prompt version (``<name>@<version>``) or a skill id —
when ``predict_target`` is no longer the generic ``llm`` completion.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | Sequence[str] | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_eval_runs",
        sa.Column("subject_ref", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_eval_runs", "subject_ref")
