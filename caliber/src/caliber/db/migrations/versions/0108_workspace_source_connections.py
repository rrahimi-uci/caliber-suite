"""add encrypted GitHub App connection storage for Workspace sources

P4-E persistence slice: store only identifiers and caliber.secret_store
``secret://name`` references for a source's GitHub App connection (app id,
installation id, private-key reference, webhook-secret reference). No
plaintext credential column exists on this table; secret material lives
exclusively in the existing encrypted secret store.

Revision ID: 0108
Revises: 0107
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0108"
down_revision: str | Sequence[str] | None = "0107"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workspace_source_connections",
        sa.Column("connection_id", sa.String(64), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_sources.source_id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("private_key_ref", sa.String(256), nullable=False),
        sa.Column("webhook_secret_ref", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'bitbucket')",
            name="ck_workspace_source_connection_provider",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_workspace_source_connection_status",
        ),
    )
    op.create_index(
        "uq_workspace_source_connection_active",
        "caliber_workspace_source_connections",
        ["source_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_workspace_source_connections_project_status",
        "caliber_workspace_source_connections",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_source_connections_project_status",
        table_name="caliber_workspace_source_connections",
    )
    op.drop_index(
        "uq_workspace_source_connection_active",
        table_name="caliber_workspace_source_connections",
    )
    op.drop_table("caliber_workspace_source_connections")
