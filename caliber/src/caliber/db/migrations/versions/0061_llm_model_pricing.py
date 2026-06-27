"""per-model LLM token pricing (gateway cost config)

Revision ID: 0061
Revises: 0060
Create Date: 2026-06-25

Adds ``caliber_llm_model_pricing`` — operator-configured per-model token rates
(USD per 1K prompt / completion / cached-prompt tokens). CALIBER computes the
``cost_usd`` it records on trace spans + refinement jobs from a per-model price
table; rows here override / extend the built-in ``DEFAULT_MODEL_PRICING`` so
operators can correct rates or add models without a code change. Surfaced + edited
in the LLM Gateway page's Pricing tab.

Scoping (``project_id`` / ``visibility``) and the lifecycle ``status`` mirror the
other CALIBER assets (judges, skills, eval datasets). One row per
(provider, model_id).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: str | Sequence[str] | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_llm_model_pricing",
        sa.Column("pricing_id", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("completion_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cached_prompt_price", sa.Float(), nullable=True),
        sa.Column("owner", sa.String(length=256), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="project"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "model_id", name="uq_llm_pricing_provider_model"),
    )


def downgrade() -> None:
    op.drop_table("caliber_llm_model_pricing")
