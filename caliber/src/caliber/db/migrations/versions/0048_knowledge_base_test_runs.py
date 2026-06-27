"""knowledge base test runs

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-18

Persist durable knowledge-base *calibration* runs — the KB analog of the prompt
(0045), tool (0046), and skill (0047) test-run tables. One row per completed run
scores a KB version against a test-question set on Recall@k, nDCG@k, Faithfulness,
and Answer-correctness; ``metrics`` is the aggregate JSON bag and ``results`` the
heavy per-question array (omitted from history summaries). ``eval_dataset_version``
pins the exact example set scored, like the prompt-run pin.

Also adds a ``baseline_run_id`` column to ``caliber_knowledge_bases`` so an
operator can pin one calibration run as the comparison baseline for the KB.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_knowledge_base_test_runs",
        sa.Column("test_run_id", sa.String(length=64), primary_key=True),
        sa.Column("knowledge_base_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_base_version_id", sa.String(length=64), nullable=False),
        sa.Column("eval_dataset_id", sa.String(length=64), nullable=True),
        sa.Column("eval_dataset_version", sa.Integer(), nullable=True),
        sa.Column("retrieval_mode", sa.String(length=16), nullable=False, server_default="dense"),
        sa.Column("top_k", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("test_set_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_kb_test_runs_kb_created",
        "caliber_knowledge_base_test_runs",
        ["knowledge_base_id", "created_at"],
    )
    op.create_index(
        "ix_kb_test_runs_created",
        "caliber_knowledge_base_test_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_caliber_knowledge_base_test_runs_knowledge_base_id",
        "caliber_knowledge_base_test_runs",
        ["knowledge_base_id"],
    )

    with op.batch_alter_table("caliber_knowledge_bases") as batch_op:
        batch_op.add_column(sa.Column("baseline_run_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_knowledge_bases") as batch_op:
        batch_op.drop_column("baseline_run_id")

    op.drop_index(
        "ix_caliber_knowledge_base_test_runs_knowledge_base_id",
        table_name="caliber_knowledge_base_test_runs",
    )
    op.drop_index(
        "ix_kb_test_runs_created",
        table_name="caliber_knowledge_base_test_runs",
    )
    op.drop_index(
        "ix_kb_test_runs_kb_created",
        table_name="caliber_knowledge_base_test_runs",
    )
    op.drop_table("caliber_knowledge_base_test_runs")
