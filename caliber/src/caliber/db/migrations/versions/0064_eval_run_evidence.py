"""immutable evidence bundle on an evaluation run

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-28

Adds ``caliber_eval_runs.evidence`` — a JSON block written once with the run that
carries the dataset/content digests, the pre-truncation sampling decision, the
per-scorer denominators behind each aggregate mean, per-tag slices, and the
resolved identity (subject content digest, judge definitions, model, provider) of
whatever produced the predictions.

The run already stored its rows, but nothing recorded *what* was graded or *how
much of it*, so a pinned run was reproducible by convention rather than by proof
and a bounded sample could be mistaken for an exhaustive one.

Nullable with no backfill: the digests and denominators can only be computed from
the run's own inputs at the time it executed. Inventing them now for historical
rows would manufacture evidence, which is exactly the failure mode this column
exists to prevent. Existing rows therefore read ``NULL``, which honestly means
"this run predates the evidence contract".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | Sequence[str] | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("caliber_eval_runs", sa.Column("evidence", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("caliber_eval_runs", "evidence")
