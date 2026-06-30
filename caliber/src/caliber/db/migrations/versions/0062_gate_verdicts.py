"""per-version advisory eval-gate verdicts

Revision ID: 0062
Revises: 0061
Create Date: 2026-06-30

Adds ``caliber_gate_verdicts`` — the version-addressable record of the latest
advisory eval-gate verdict for an artifact version. The gate is advisory in v1
(no rotation-boundary enforcement); the Version panel reads this to show
PASS/FAIL/none before a promotion, and the audited prompt promote stamps it from
the operator-supplied verdict. ``state`` is authoritative (both the aggregate
floor and the per-dimension regression rule); numeric columns are display
detail. One row per ``(artifact_type, version_key)`` — upserted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | Sequence[str] | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_gate_verdicts",
        sa.Column("gate_verdict_id", sa.String(length=64), primary_key=True),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("version_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("baseline_score", sa.Float(), nullable=True),
        sa.Column("min_aggregate_score", sa.Float(), nullable=True),
        sa.Column("worst_regression", sa.Float(), nullable=True),
        sa.Column("max_regression_delta", sa.Float(), nullable=True),
        sa.Column("eval_run_id", sa.String(length=64), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "artifact_type", "version_key", name="uq_gate_verdict_artifact_version"
        ),
    )


def downgrade() -> None:
    op.drop_table("caliber_gate_verdicts")
