"""Portable Workspace revision allocation and retention invariants.

This module is intentionally small and provider-free. P4-A establishes the
database contract that later import and Change Request services consume; it
does not materialize archives, call Git providers, or expose public routes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
    WorkspaceRevisionImmutableError,
    WorkspaceSnapshotRetentionError,
)

REVISION_STATUS_VALIDATING = "validating"
REVISION_STATUS_READY = "ready"
REVISION_STATUS_INVALID = "invalid"
REVISION_STATUSES = frozenset(
    {REVISION_STATUS_VALIDATING, REVISION_STATUS_READY, REVISION_STATUS_INVALID}
)
TERMINAL_REVISION_STATUSES = frozenset({REVISION_STATUS_READY, REVISION_STATUS_INVALID})


class RevisionAllocationError(RuntimeError):
    """Raised when the project revision counter cannot be allocated."""


def allocate_revision_number(session: Session, project_id: str, *, max_attempts: int = 8) -> int:
    """Atomically reserve the next revision number for ``project_id``.

    The read-then-conditional-update sequence deliberately avoids
    ``SELECT ... FOR UPDATE`` and ``UPDATE ... RETURNING``. Both are either
    unavailable or subtly different on supported SQLite versions, while the
    conditional update has the same compare-and-set semantics on SQLite and
    PostgreSQL. A failed attempt simply reloads the counter; the caller owns
    the surrounding transaction and may commit the reservation together with
    its revision row.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    for _attempt in range(max_attempts):
        expected = session.scalar(
            select(CaliberProject.next_revision_number).where(
                CaliberProject.project_id == project_id
            )
        )
        if expected is None:
            raise RevisionAllocationError(f"project {project_id!r} was not found")
        if expected < 1:
            raise RevisionAllocationError(
                f"project {project_id!r} has invalid next revision number {expected!r}"
            )

        result = cast(
            CursorResult[Any],
            session.execute(
                update(CaliberProject)
                .where(
                    CaliberProject.project_id == project_id,
                    CaliberProject.next_revision_number == expected,
                )
                .values(next_revision_number=CaliberProject.next_revision_number + 1)
            ),
        )
        if result.rowcount == 1:
            return int(expected)

    raise RevisionAllocationError(
        f"could not allocate a revision number for project {project_id!r} "
        f"after {max_attempts} compare-and-set attempts"
    )


def finalize_revision(
    _session: Session,
    revision: CaliberWorkspaceRevision,
    status: str,
    *,
    validation_report: Mapping[str, Any] | None = None,
    validated_by: str | None = None,
) -> CaliberWorkspaceRevision:
    """Move a validating revision to one immutable terminal state."""
    if status not in TERMINAL_REVISION_STATUSES:
        raise ValueError(
            f"terminal revision status must be one of {sorted(TERMINAL_REVISION_STATUSES)}"
        )
    if revision.status != REVISION_STATUS_VALIDATING:
        raise WorkspaceRevisionImmutableError(
            f"workspace revision {revision.revision_id!r} is already {revision.status!r}"
        )

    revision.status = status
    revision.validation_report = dict(validation_report) if validation_report is not None else None
    revision.validated_by = validated_by
    revision.validated_at = datetime.now(timezone.utc)
    return revision


def assert_revision_mutable(revision: CaliberWorkspaceRevision) -> None:
    """Raise if a revision may no longer receive pins or validation changes."""
    if revision.status in TERMINAL_REVISION_STATUSES:
        raise WorkspaceRevisionImmutableError(
            f"workspace revision {revision.revision_id!r} is terminal ({revision.status})"
        )
    if revision.status != REVISION_STATUS_VALIDATING:
        raise WorkspaceRevisionImmutableError(
            f"workspace revision {revision.revision_id!r} has unknown mutable state "
            f"{revision.status!r}"
        )


def assert_snapshot_deletable(session: Session, snapshot_file_id: str) -> None:
    """Guard garbage collection of a snapshot referenced by a ready revision.

    Invalid terminal revisions remain auditable but do not retain physical
    snapshots. A ready revision retains both its canonical source snapshot and
    every resource snapshot, matching the Workspace plan's retention rule.
    """
    direct_revision_id = session.scalar(
        select(CaliberWorkspaceRevision.revision_id)
        .where(
            CaliberWorkspaceRevision.status == REVISION_STATUS_READY,
            CaliberWorkspaceRevision.source_snapshot_file_id == snapshot_file_id,
        )
        .limit(1)
    )
    resource_revision_id = session.scalar(
        select(CaliberWorkspaceRevision.revision_id)
        .join(
            CaliberWorkspaceRevisionResource,
            CaliberWorkspaceRevisionResource.revision_id == CaliberWorkspaceRevision.revision_id,
        )
        .where(
            CaliberWorkspaceRevision.status == REVISION_STATUS_READY,
            CaliberWorkspaceRevisionResource.snapshot_file_id == snapshot_file_id,
        )
        .limit(1)
    )
    if direct_revision_id is not None or resource_revision_id is not None:
        revision_id = direct_revision_id or resource_revision_id
        raise WorkspaceSnapshotRetentionError(
            f"snapshot file {snapshot_file_id!r} is retained by ready revision {revision_id!r}"
        )


__all__ = [
    "REVISION_STATUSES",
    "REVISION_STATUS_INVALID",
    "REVISION_STATUS_READY",
    "REVISION_STATUS_VALIDATING",
    "TERMINAL_REVISION_STATUSES",
    "RevisionAllocationError",
    "WorkspaceRevisionImmutableError",
    "WorkspaceSnapshotRetentionError",
    "allocate_revision_number",
    "assert_revision_mutable",
    "assert_snapshot_deletable",
    "finalize_revision",
]
