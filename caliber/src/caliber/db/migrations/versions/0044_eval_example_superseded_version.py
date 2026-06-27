"""eval example superseded_version

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-17

Adds ``superseded_version`` to ``caliber_eval_dataset_examples``. The
column records the dataset version at which an example was retired (the
``superseded_at`` timestamp alone can't be compared against a pinned
integer version). With it, a prompt-optimization run that pins
``eval_dataset_version = N`` can reconstruct the exact active set the
dataset contained at version N: include rows with
``dataset_version <= N`` and exclude rows whose ``superseded_version``
is non-null and ``<= N``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | Sequence[str] | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_eval_dataset_examples",
        sa.Column("superseded_version", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_eval_dataset_examples", "superseded_version")
