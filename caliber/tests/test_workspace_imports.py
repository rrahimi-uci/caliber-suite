"""Tests for the provider-neutral Workspace import-job lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceSource,
)
from caliber.workspace_imports import (
    MAX_IMPORT_ERROR_SUMMARY_CHARS,
    WorkspaceImportLeaseLostError,
    WorkspaceImportTransitionError,
    claim_next_import_job,
    finish_import_job,
    heartbeat_import_job,
    reconcile_expired_import_jobs,
)

NOW = datetime(2026, 9, 16, 12, 0, 0)


def _project_and_source(session: Session, project_id: str = "PRJ-import") -> None:
    session.add(CaliberProject(project_id=project_id, name=project_id))
    session.flush()
    session.add(
        CaliberWorkspaceSource(
            source_id=f"WSS-{project_id}",
            project_id=project_id,
            provider="github",
            provider_host="github.com",
            canonical_repository_id=f"repo-{project_id}",
            display_path="owner/repo",
        )
    )
    session.flush()


def _job(
    session: Session,
    import_job_id: str,
    *,
    project_id: str = "PRJ-import",
    created_at: datetime = NOW,
    status: str = "queued",
) -> CaliberWorkspaceImportJob:
    row = CaliberWorkspaceImportJob(
        import_job_id=import_job_id,
        project_id=project_id,
        source_id=f"WSS-{project_id}",
        repository="owner/repo",
        commit_sha="a" * 40,
        idempotency_key=import_job_id,
        created_by="@author",
        created_at=created_at,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def test_claim_chooses_oldest_job_and_seeds_lease(db_session: Session) -> None:
    _project_and_source(db_session)
    _job(db_session, "WSI-new", created_at=NOW + timedelta(minutes=1))
    _job(db_session, "WSI-old", created_at=NOW)
    db_session.commit()

    claimed = claim_next_import_job(
        db_session,
        worker_id="import-worker-1",
        lease_seconds=45,
        now=NOW.replace(tzinfo=timezone.utc),
    )

    assert claimed is not None
    assert claimed.import_job_id == "WSI-old"
    assert claimed.status == "running"
    assert claimed.claimed_by == "import-worker-1"
    assert claimed.claimed_at == NOW
    assert claimed.last_heartbeat_at == NOW
    assert claimed.lease_expires_at == NOW + timedelta(seconds=45)


def test_claim_returns_none_when_queue_is_empty_or_only_nonqueued(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-empty")
    _job(db_session, "WSI-running", project_id="PRJ-empty", status="running")
    db_session.commit()

    assert claim_next_import_job(db_session, worker_id="worker", now=NOW) is None


def test_claim_race_has_one_winner(session_factory) -> None:
    """The conditional UPDATE arbitrates a SELECT/UPDATE interleaving."""
    with session_factory() as session:
        session.add(CaliberProject(project_id="PRJ-race", name="PRJ-race"))
        session.flush()
        session.add(
            CaliberWorkspaceSource(
                source_id="WSS-PRJ-race",
                project_id="PRJ-race",
                provider="github",
                provider_host="github.com",
                canonical_repository_id="repo-race",
                display_path="owner/repo",
            )
        )
        session.flush()
        _job(session, "WSI-race", project_id="PRJ-race")
        session.commit()

    class _RacingSession:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._selected = False

        def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = self._inner.execute(statement, *args, **kwargs)
            if not self._selected:
                self._selected = True
                self._inner.commit()
                with session_factory() as rival:
                    claim_next_import_job(rival, worker_id="rival", now=NOW)
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    with session_factory() as inner:
        loser = claim_next_import_job(_RacingSession(inner), worker_id="slow", now=NOW)

    assert loser is None
    with session_factory() as session:
        row = session.get(CaliberWorkspaceImportJob, "WSI-race")
        assert row is not None and row.claimed_by == "rival"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"worker_id": ""}, "worker_id"),
        ({"worker_id": "w", "lease_seconds": 0}, "lease_seconds"),
        ({"worker_id": "w", "lease_seconds": float("inf")}, "lease_seconds"),
        ({"worker_id": "w", "lease_seconds": 3601}, "at most"),
    ],
)
def test_claim_validates_worker_and_lease_inputs(
    db_session: Session, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        claim_next_import_job(db_session, now=NOW, **kwargs)  # type: ignore[arg-type]


def test_claim_rejects_oversized_worker_and_non_numeric_lease(db_session: Session) -> None:
    with pytest.raises(ValueError, match="at most"):
        claim_next_import_job(db_session, worker_id="w" * 129, now=NOW)
    with pytest.raises(ValueError, match="finite"):
        claim_next_import_job(db_session, worker_id="worker", lease_seconds="60", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        claim_next_import_job(db_session, worker_id="worker", lease_seconds=True, now=NOW)  # type: ignore[arg-type]


def test_heartbeat_is_owner_and_expiry_fenced(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-heartbeat")
    _job(db_session, "WSI-heartbeat", project_id="PRJ-heartbeat")
    db_session.commit()
    claim_next_import_job(db_session, worker_id="worker-a", lease_seconds=60, now=NOW)

    renewed = heartbeat_import_job(
        db_session,
        "WSI-heartbeat",
        worker_id="worker-a",
        lease_seconds=90,
        now=NOW + timedelta(seconds=30),
    )
    assert renewed.last_heartbeat_at == NOW + timedelta(seconds=30)
    assert renewed.lease_expires_at == NOW + timedelta(seconds=120)

    with pytest.raises(WorkspaceImportLeaseLostError, match="no longer owns"):
        heartbeat_import_job(
            db_session,
            "WSI-heartbeat",
            worker_id="worker-b",
            now=NOW + timedelta(seconds=31),
        )
    with pytest.raises(WorkspaceImportLeaseLostError, match="no longer owns"):
        heartbeat_import_job(
            db_session,
            "WSI-heartbeat",
            worker_id="worker-a",
            now=NOW + timedelta(seconds=121),
        )


def test_finish_success_requires_revision_and_rejects_late_worker(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-finish")
    _job(db_session, "WSI-finish", project_id="PRJ-finish")
    db_session.commit()
    claim_next_import_job(db_session, worker_id="worker-a", lease_seconds=60, now=NOW)

    with pytest.raises(ValueError, match="revision_id"):
        finish_import_job(
            db_session, "WSI-finish", worker_id="worker-a", status="succeeded", now=NOW
        )

    finished = finish_import_job(
        db_session,
        "WSI-finish",
        worker_id="worker-a",
        status="succeeded",
        revision_id="WSR-finish",
        now=NOW + timedelta(seconds=10),
    )
    assert finished.status == "succeeded"
    assert finished.revision_id == "WSR-finish"
    assert finished.lease_expires_at is None
    assert finished.completed_at == NOW + timedelta(seconds=10)

    with pytest.raises(WorkspaceImportLeaseLostError):
        heartbeat_import_job(db_session, "WSI-finish", worker_id="worker-a", now=NOW)


def test_finish_rejects_success_error_details_and_late_lease(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-late")
    _job(db_session, "WSI-late", project_id="PRJ-late")
    db_session.commit()
    claim_next_import_job(db_session, worker_id="worker-a", lease_seconds=1, now=NOW)

    with pytest.raises(WorkspaceImportTransitionError, match="error details"):
        finish_import_job(
            db_session,
            "WSI-late",
            worker_id="worker-a",
            status="succeeded",
            revision_id="WSR-late",
            error_code="unexpected",
            now=NOW,
        )
    with pytest.raises(WorkspaceImportLeaseLostError, match="no longer owns"):
        finish_import_job(
            db_session,
            "WSI-late",
            worker_id="worker-a",
            status="succeeded",
            revision_id="WSR-late",
            now=NOW + timedelta(seconds=2),
        )


def test_terminal_transition_contract_is_closed(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-transition")
    _job(db_session, "WSI-transition", project_id="PRJ-transition")
    db_session.commit()
    claim_next_import_job(db_session, worker_id="worker-a", now=NOW)

    with pytest.raises(WorkspaceImportTransitionError) as exc_info:
        finish_import_job(
            db_session, "WSI-transition", worker_id="worker-a", status="queued", now=NOW
        )
    assert exc_info.value.code == "invalid_import_job_transition"


def test_finish_failure_requires_bounded_error_code_and_summary(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-failure")
    _job(db_session, "WSI-failure", project_id="PRJ-failure")
    db_session.commit()
    claim_next_import_job(db_session, worker_id="worker-a", now=NOW)

    with pytest.raises(ValueError, match="error_code is required"):
        finish_import_job(db_session, "WSI-failure", worker_id="worker-a", status="failed", now=NOW)
    with pytest.raises(ValueError, match="at most"):
        finish_import_job(
            db_session,
            "WSI-failure",
            worker_id="worker-a",
            status="failed",
            error_code="import_failed",
            error_summary="x" * (MAX_IMPORT_ERROR_SUMMARY_CHARS + 1),
            now=NOW,
        )
    with pytest.raises(ValueError, match="at most"):
        finish_import_job(
            db_session,
            "WSI-failure",
            worker_id="worker-a",
            status="failed",
            error_code="x" * 65,
            now=NOW,
        )
    with pytest.raises(ValueError, match="must be a string"):
        finish_import_job(
            db_session,
            "WSI-failure",
            worker_id="worker-a",
            status="failed",
            error_code="import_failed",
            error_summary=123,  # type: ignore[arg-type]
            now=NOW,
        )

    failed = finish_import_job(
        db_session,
        "WSI-failure",
        worker_id="worker-a",
        status="failed",
        error_code="manifest_invalid",
        error_summary="manifest could not be parsed",
        now=NOW + timedelta(seconds=1),
    )
    assert failed.status == "failed"
    assert failed.error_code == "manifest_invalid"


def test_expired_leases_require_reconciliation_and_are_not_retried(db_session: Session) -> None:
    _project_and_source(db_session, "PRJ-reconcile")
    _job(db_session, "WSI-expired", project_id="PRJ-reconcile")
    _job(
        db_session,
        "WSI-fresh",
        project_id="PRJ-reconcile",
        created_at=NOW + timedelta(seconds=1),
    )
    db_session.commit()
    claim_next_import_job(db_session, worker_id="worker-a", lease_seconds=10, now=NOW)
    # Claim the second row too, so the sweep proves it only moves the expired lease.
    claim_next_import_job(db_session, worker_id="worker-b", lease_seconds=100, now=NOW)

    reconciled = reconcile_expired_import_jobs(
        db_session, now=NOW + timedelta(seconds=11), actor="reconciler"
    )

    assert reconciled == ["WSI-expired"]
    expired = db_session.get(CaliberWorkspaceImportJob, "WSI-expired")
    fresh = db_session.get(CaliberWorkspaceImportJob, "WSI-fresh")
    assert expired is not None and expired.status == "reconcile_required"
    assert expired.error_code == "import_lease_expired"
    assert expired.lease_expires_at is None
    assert expired.last_heartbeat_at == NOW
    assert fresh is not None and fresh.status == "running"


def test_reconcile_limit_and_actor_validation(db_session: Session) -> None:
    with pytest.raises(ValueError, match="limit"):
        reconcile_expired_import_jobs(db_session, limit=0)
    with pytest.raises(ValueError, match="worker_id"):
        reconcile_expired_import_jobs(db_session, actor="")


def test_aware_datetimes_are_normalized_to_database_clock() -> None:
    aware = datetime(2026, 9, 16, 5, 0, 0, tzinfo=timezone.utc)
    assert aware.astimezone(timezone.utc).replace(tzinfo=None) == NOW - timedelta(hours=7)
