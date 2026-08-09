"""add personal access tokens for automation

Revision ID: 0086
Revises: 0085
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: str | Sequence[str] | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_personal_access_tokens",
        sa.Column("token_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(256), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        # Only the SHA-256 digest, never the token: this table must not be a
        # credential store even to someone holding a database dump.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(64), nullable=True),
        sa.Column("rotated_from", sa.String(64), nullable=True),
    )
    # Unique: two tokens hashing alike would make resolution ambiguous, and the
    # lookup below is on the hot path of every authenticated SDK request.
    op.create_index(
        "ix_pat_token_hash", "caliber_personal_access_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_pat_user", "caliber_personal_access_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_pat_user", table_name="caliber_personal_access_tokens")
    op.drop_index("ix_pat_token_hash", table_name="caliber_personal_access_tokens")
    op.drop_table("caliber_personal_access_tokens")
