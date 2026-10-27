"""add the exclusive experiment owner registry

`B11.registry` (#665): ownership of a tracking-backend experiment decides who sees its traces, so it is recorded once,
exclusively, on the backend's own canonical id. ``caliber_experiment_owners`` is keyed by ``(tracking_backend,
experiment_id)``: the primary key is what stops two projects both claiming one experiment, whichever alias each used.

The table starts empty. Existing agent and workflow claims are text and cannot be resolved to a canonical id from a
migration (that needs the tracking backend), so they are migrated by ``caliber.observability.experiment_registry.
migrate_legacy_claims`` (an administrator's call, ``POST /observability/experiment-claims/migrate``): an experiment
claimed by one scope becomes a ``legacy`` row, one claimed by two or more becomes a ``quarantined`` row with no owner.
Until it has run, the claim-derived visibility in ``caliber.observability.trace_access`` still applies, so a foreign
payload is denied before and during the migration. Downgrade drops the table.

Revision ID: 0118
Revises: 0117
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0118"
down_revision: str | Sequence[str] | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_experiment_owners",
        sa.Column("tracking_backend", sa.String(length=512), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.String(length=16), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_by", sa.String(length=256), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("tracking_backend", "experiment_id"),
        sa.CheckConstraint("status IN ('active', 'quarantined')", name="ck_experiment_owner_status"),
        sa.CheckConstraint(
            "provenance IN ('created', 'adopted', 'legacy', 'resolved')",
            name="ck_experiment_owner_provenance",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND ((scope_kind IN ('project', 'user') AND scope_id <> '') OR "
            "(scope_kind = 'public' AND scope_id = ''))) OR "
            "(status = 'quarantined' AND scope_kind = '' AND scope_id = '')",
            name="ck_experiment_owner_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("caliber_experiment_owners")
