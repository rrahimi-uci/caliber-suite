"""component calibration test cases

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-17

Adds persisted calibration test cases + last-result storage for tools and MCP
tools. Tools store a flat list of cases (``test_cases``) plus the latest scored
result (``last_calibration``). MCP servers store cases keyed by tool name
(``tool_test_cases``) plus the latest result per tool (``tool_calibrations``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_tool_registry") as batch_op:
        batch_op.add_column(
            sa.Column("test_cases", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("last_calibration", sa.JSON(), nullable=True))
    with op.batch_alter_table("caliber_mcp_servers") as batch_op:
        batch_op.add_column(
            sa.Column("tool_test_cases", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("tool_calibrations", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_mcp_servers") as batch_op:
        batch_op.drop_column("tool_calibrations")
        batch_op.drop_column("tool_test_cases")
    with op.batch_alter_table("caliber_tool_registry") as batch_op:
        batch_op.drop_column("last_calibration")
        batch_op.drop_column("test_cases")
