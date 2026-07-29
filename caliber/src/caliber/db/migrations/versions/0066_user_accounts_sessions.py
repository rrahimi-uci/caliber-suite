"""server-validated password accounts and revocable sessions

Revision ID: 0066
Revises: 0065
Create Date: 2026-07-28

Closes C1. Identity was client-asserted: the SPA validated ``admin/admin`` in the
browser, sent the result as ``X-CALIBER-User``, and the backend trusted it.

``caliber_user_accounts`` holds a scrypt password hash CALIBER verifies itself.
``caliber_sessions`` holds revocable sessions — a table rather than a stateless
signed token, because a signed token cannot be revoked before expiry and that makes
"disable this account now" and "log out everywhere" impossible. Only a SHA-256
fingerprint of each token is stored, so reading the table yields no usable
credential.

No accounts are created by this migration. An existing deployment keeps working by
setting ``CALIBER_AUTH_MODE=trusted_header`` (its de-facto current posture, now
explicit) and can move to session auth by seeding a bootstrap admin.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_user_accounts",
        sa.Column("user_id", sa.String(length=256), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("password_updated_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "caliber_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
    )
    op.create_index("ix_sessions_token_hash", "caliber_sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_user", "caliber_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user", table_name="caliber_sessions")
    op.drop_index("ix_sessions_token_hash", table_name="caliber_sessions")
    op.drop_table("caliber_sessions")
    op.drop_table("caliber_user_accounts")
