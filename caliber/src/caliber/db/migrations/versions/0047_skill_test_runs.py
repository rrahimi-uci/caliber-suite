"""skill test runs

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-18

Persist durable skill-test runs from the Skills tab so each run and its per-case
verdicts survive a page refresh — the skill analog of the prompt (0045) and tool
(0046) test-run tables. One row per completed run; ``kind`` records the producing
surface (selection trigger test, variable-render preview, or scenario suite).
``results`` holds the full per-case array; the scalar count/score columns are
server-recomputed summaries for cheap history listing.

Skill testing/calibration uses an auto-provisioned *hidden skill target*
(a ``caliber_agent_config`` row keyed ``skill::{name}``) for its runtime
identity, and pins the comparison baseline on that target's ``optimizer_config``
(like the prompt target) — so no extra ``baseline_run_id`` column is needed here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_skill_test_runs",
        sa.Column("test_run_id", sa.String(length=64), primary_key=True),
        sa.Column("skill_id", sa.String(length=64), nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="scenario"),
        sa.Column("test_set_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partial_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("host_agent_id", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("mlflow_run_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_skill_test_runs_skill_created",
        "caliber_skill_test_runs",
        ["skill_id", "created_at"],
    )
    op.create_index(
        "ix_skill_test_runs_created",
        "caliber_skill_test_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_caliber_skill_test_runs_skill_id",
        "caliber_skill_test_runs",
        ["skill_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caliber_skill_test_runs_skill_id",
        table_name="caliber_skill_test_runs",
    )
    op.drop_index(
        "ix_skill_test_runs_created",
        table_name="caliber_skill_test_runs",
    )
    op.drop_index(
        "ix_skill_test_runs_skill_created",
        table_name="caliber_skill_test_runs",
    )
    op.drop_table("caliber_skill_test_runs")
