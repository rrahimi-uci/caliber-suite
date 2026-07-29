"""per-service rate limit and CORS policy

Revision ID: 0071
Revises: 0070
Create Date: 2026-07-29

Adds ``rate_limit_per_minute`` and ``cors_allowed_origins`` to
``caliber_workflow_services``. A UI-published service was authenticated but
otherwise unbounded: any token holder could drive unlimited traffic through a
workflow that calls paid model APIs, and the report tracked "no rate-limit, quota,
CORS" as an open gap on the published-API surface.

Both default to the permissive-but-explicit end for a reason:

* ``rate_limit_per_minute = 0`` means unlimited, so an upgrade does not begin
  refusing traffic for services that were working yesterday. An operator opts into a
  ceiling.
* ``cors_allowed_origins = ''`` means **no** CORS headers are emitted, which is the
  *restrictive* choice. Defaulting to a wildcard would let any website read a
  token-authorized response, so "unset" has to mean "no browser access" rather than
  "all browser access".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | Sequence[str] | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_workflow_services",
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "caliber_workflow_services",
        sa.Column("cors_allowed_origins", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("caliber_workflow_services", "cors_allowed_origins")
    op.drop_column("caliber_workflow_services", "rate_limit_per_minute")
