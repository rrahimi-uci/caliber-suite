"""add project visibility to MCP server registry

`P2-O` (docs/workspace-plan.md Phase 2 item 5): MCP servers are workspace
resources just like skills, tools, and workflows. Existing rows remain private
to their recorded owner and unbound to a project; newly created rows use the
request identity's active project when one is present. The global server name
constraint is retained as a compatibility boundary for existing integrations.

Revision ID: 0096
Revises: 0095
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0096"
down_revision: str | Sequence[str] | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_mcp_servers",
        sa.Column("project_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "caliber_mcp_servers",
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="user"),
    )
    op.create_index(
        "ix_mcp_servers_project_visibility",
        "caliber_mcp_servers",
        ["project_id", "visibility"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_project_visibility", table_name="caliber_mcp_servers")
    op.drop_column("caliber_mcp_servers", "visibility")
    op.drop_column("caliber_mcp_servers", "project_id")
