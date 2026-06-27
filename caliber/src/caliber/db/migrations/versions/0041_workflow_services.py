"""workflow services

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-17

Persist "deploy workflow as a service" config: a per-workflow service row with
its derived invocation schema, plus per-service Bearer tokens (only the SHA-256
hash is stored) that authorize external run-and-poll invocations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workflow_services",
        sa.Column("service_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflows.workflow_id"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(length=64), nullable=False, server_default="prod"),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auth_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workflow_id", name="uq_workflow_service_workflow_id"),
    )
    op.create_table(
        "caliber_service_tokens",
        sa.Column("token_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflows.workflow_id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_service_token_hash"),
    )
    op.create_index(
        "ix_service_tokens_workflow",
        "caliber_service_tokens",
        ["workflow_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_service_tokens_workflow", table_name="caliber_service_tokens")
    op.drop_table("caliber_service_tokens")
    op.drop_table("caliber_workflow_services")
