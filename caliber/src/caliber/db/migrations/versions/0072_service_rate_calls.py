"""shared per-service rate-limit accounting

Revision ID: 0072
Revises: 0071
Create Date: 2026-07-30

The service work budget was counted in a process-local dict, so ``rate_limit_per_minute``
meant *per replica*: three replicas granted three times the configured ceiling. Every
review since the limiter landed has recorded that as open, and the source said so at the
definition rather than fixing it.

One row per charged invocation, deleted once it leaves the window. That keeps the sliding
60-second semantics the config describes, rather than switching to a cheaper fixed window
that would allow up to 2x the limit across a boundary.

The volume is bounded by the limit itself: a service capped at N/minute holds at most
about N rows. Unlimited services (``0``, the default) are never charged and write nothing,
so the common case costs no writes at all.

**Not a distributed lock.** Two replicas can both read a count under the ceiling and both
insert, so a burst can exceed the limit by roughly the number of concurrent replicas. This
is a spend guard on paid model calls, not an authorization boundary, and a brief overshoot
of a few requests is an acceptable trade against taking a row lock on every invocation.
Stated here rather than implied, because the previous implementation's honesty about its
own limits is what made this fixable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | Sequence[str] | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_service_rate_calls",
        sa.Column("call_id", sa.String(length=64), primary_key=True),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("called_at", sa.DateTime(), nullable=False),
    )
    # The exact predicate the limiter uses: count this service's calls inside the window,
    # then delete what has aged out.
    op.create_index(
        "ix_caliber_service_rate_calls_service_time",
        "caliber_service_rate_calls",
        ["service_id", "called_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caliber_service_rate_calls_service_time",
        table_name="caliber_service_rate_calls",
    )
    op.drop_table("caliber_service_rate_calls")
