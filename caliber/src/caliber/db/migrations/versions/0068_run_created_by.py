"""record who started a workflow run

Revision ID: 0068
Revises: 0067
Create Date: 2026-07-28

Adds ``caliber_workflow_runs.created_by``. Nothing on the run recorded its initiator,
which blocked two things:

* **Separation of duties on human-approval gates.** "The account that triggered this
  run cannot approve it" is the one SoD rule that is meaningful without a role
  hierarchy, and it needs to know who triggered the run.
* **Answering "who ran this?"** at all, without reconstructing it from the audit log.

Nullable with no backfill: the initiator of a historical run is not recoverable from
the run row, and guessing would put a wrong name on an audit-relevant field. A run
with a null initiator is treated as "unknown", which permits approval rather than
deadlocking an in-flight run at upgrade time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: str | Sequence[str] | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_workflow_runs", sa.Column("created_by", sa.String(length=256), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("caliber_workflow_runs", "created_by")
