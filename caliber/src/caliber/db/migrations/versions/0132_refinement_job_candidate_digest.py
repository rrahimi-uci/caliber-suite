"""record the digest of the candidate that cleared the eval gate

`B06.4` (#753): ``caliber_refinement_jobs.candidate_digest`` is the ``sha256:`` digest of the job's candidate at the
moment the eval gate cleared (``candidate_ready``). The Apply claim recomputes the digest of the candidate it is about
to promote and refuses on a mismatch, so a candidate edited after the gate is never promoted on the strength of an
evaluation of different content.

The column is nullable and the migration is additive, safe for rolling deployment: an older writer leaves it NULL and
an older reader ignores it. Jobs already waiting to be applied (``candidate_ready``) or mid-claim (``applying``, which
returns to ``candidate_ready`` on a failed release) are backfilled from their current candidate: at upgrade time that is
the only evaluated candidate the row can name, and leaving them NULL would strand every in-flight candidate behind a
refusal. A candidate that cannot be digested (no candidate, or not a JSON object) stays NULL and is refused at Apply.
Every other status is left NULL. The downgrade drops only the column.

Revision ID: 0132
Revises: 0131
Create Date: 2026-10-31
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0132"
down_revision: str | Sequence[str] | None = "0131"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The digest algorithm is inlined (not imported from ``caliber.candidate_digest``) so this shipped migration keeps
# producing the same bytes even if the runtime helper is ever revised.
_BACKFILL_STATUSES = ("candidate_ready", "applying")

_jobs = sa.table(
    "caliber_refinement_jobs",
    sa.column("job_id", sa.String),
    sa.column("status", sa.String),
    sa.column("candidate", sa.JSON),
    sa.column("candidate_digest", sa.String),
)


def _digest(candidate: Any) -> str | None:
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except ValueError:
            return None
    if not isinstance(candidate, dict) or not candidate:
        return None
    try:
        canonical = json.dumps(
            candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def upgrade() -> None:
    op.add_column(
        "caliber_refinement_jobs",
        sa.Column("candidate_digest", sa.String(length=71), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_jobs.c.job_id, _jobs.c.candidate).where(_jobs.c.status.in_(_BACKFILL_STATUSES))
    ).all()
    for job_id, candidate in rows:
        digest = _digest(candidate)
        if digest is not None:
            bind.execute(
                sa.update(_jobs).where(_jobs.c.job_id == job_id).values(candidate_digest=digest)
            )


def downgrade() -> None:
    op.drop_column("caliber_refinement_jobs", "candidate_digest")
