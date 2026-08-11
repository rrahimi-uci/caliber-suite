"""add openapi tool drafts and declarative tool backends

Revision ID: 0088
Revises: 0087
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0088"
down_revision: str | Sequence[str] | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_tool_registry",
        sa.Column(
            "execution_backend",
            sa.String(32),
            nullable=False,
            server_default="python_callable",
        ),
    )
    op.add_column(
        "caliber_tool_registry",
        sa.Column("backend_config", sa.JSON(), nullable=True),
    )

    op.create_table(
        "caliber_openapi_tool_drafts",
        sa.Column("draft_id", sa.String(64), primary_key=True),
        sa.Column(
            "integration_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_integrations.integration_id"),
            nullable=False,
        ),
        sa.Column(
            "integration_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_integration_versions.version_id"),
            nullable=False,
        ),
        sa.Column(
            "operation_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_operations.operation_id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("server_url", sa.String(1024), nullable=False, server_default=""),
        sa.Column("auth_binding", sa.JSON(), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=True),
        sa.Column("output_schema", sa.JSON(), nullable=True),
        sa.Column("execution_config", sa.JSON(), nullable=True),
        sa.Column("side_effect_level", sa.String(16), nullable=False, server_default="read"),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_in_preview", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("secret_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("published_tool_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_openapi_tool_drafts_integration",
        "caliber_openapi_tool_drafts",
        ["integration_id", "created_at"],
    )
    op.create_index(
        "ix_openapi_tool_drafts_status",
        "caliber_openapi_tool_drafts",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_openapi_tool_drafts_status", table_name="caliber_openapi_tool_drafts")
    op.drop_index(
        "ix_openapi_tool_drafts_integration", table_name="caliber_openapi_tool_drafts"
    )
    op.drop_table("caliber_openapi_tool_drafts")

    op.drop_column("caliber_tool_registry", "backend_config")
    op.drop_column("caliber_tool_registry", "execution_backend")
