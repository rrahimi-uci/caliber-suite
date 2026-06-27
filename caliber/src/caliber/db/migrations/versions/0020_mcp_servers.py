"""mcp servers

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-02

Adds the caliber_mcp_servers table for managing MCP server connections.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_mcp_servers",
        sa.Column("server_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("transport", sa.String(32), nullable=False, server_default="stdio"),
        sa.Column("uri", sa.String(1024), nullable=False, server_default=""),
        sa.Column("command", sa.String(1024), nullable=False, server_default=""),
        sa.Column("args", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("env", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("headers", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("auth_type", sa.String(32), nullable=False, server_default="none"),
        sa.Column("auth_config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("discovered_tools", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("icon", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_connected_at", sa.DateTime, nullable=True),
        sa.Column("connection_error", sa.Text, nullable=True),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_mcp_server_name"),
    )
    op.create_index("ix_mcp_server_status", "caliber_mcp_servers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_mcp_server_status", table_name="caliber_mcp_servers")
    op.drop_table("caliber_mcp_servers")
