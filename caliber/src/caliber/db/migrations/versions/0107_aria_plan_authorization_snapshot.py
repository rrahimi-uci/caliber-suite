"""add a first-class scope/role authorization snapshot to Aria plans/steps

`P2-E` (docs/workspace-plan.md section 16, item 7's residual scope): the
already-delivered part of item 7 wired `require_project_access_if_scoped`/
`resource.execute` onto `routes/aria_plans.py::execute_plan`/`poll_plan` and
`assistant/agent_tools.py::_dispatch_capability` onto `scopes_for_user`, but
every one of those checks was re-derived live on each call -- nothing was
persisted onto the `CaliberAriaPlan`/`CaliberAriaPlanStep` rows themselves.
This migration adds the columns for that snapshot, mirroring
`CaliberWorkspaceReleaseDecision.actor_role_snapshot`/
`.effective_scope_snapshot`'s shape:

* `caliber_aria_plans.actor_role_snapshot` / `.effective_scope_snapshot` /
  `.authorization_recorded_at` -- recorded once, the first time
  `execute_plan`/`poll_plan` clears the plan to run (see
  `db/models.py::CaliberAriaPlan`'s own docstring for the exact semantics).
* `caliber_aria_plan_steps.capability_scope_decision` -- the step-level
  counterpart, recorded by `PlanExecutor` wherever it already checks a
  capability's declared `required_scopes` against the plan owner.

All four columns are additive and nullable; no backfill is needed since every
existing row simply gains `NULL`s (unauthorized-until-now history is not
retroactively fabricated).

Revision ID: 0107
Revises: 0106
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0107"
down_revision: str | Sequence[str] | None = "0106"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_aria_plans") as batch_op:
        batch_op.add_column(sa.Column("actor_role_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("effective_scope_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("authorization_recorded_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("caliber_aria_plan_steps") as batch_op:
        batch_op.add_column(sa.Column("capability_scope_decision", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_aria_plan_steps") as batch_op:
        batch_op.drop_column("capability_scope_decision")
    with op.batch_alter_table("caliber_aria_plans") as batch_op:
        batch_op.drop_column("authorization_recorded_at")
        batch_op.drop_column("effective_scope_snapshot")
        batch_op.drop_column("actor_role_snapshot")
