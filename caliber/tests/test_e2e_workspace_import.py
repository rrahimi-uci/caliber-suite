"""Offline end-to-end Workspace import journey across the P4-A/P4-B seams."""

from __future__ import annotations

import hashlib
import zipfile
from datetime import datetime, timedelta
from io import BytesIO

from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceSource,
)
from caliber.workspace_imports import (
    claim_next_import_job,
    finish_import_job,
    retry_failed_import_job,
)
from caliber.workspace_manifest import manifest_digest, parse_workspace_manifest
from caliber.workspace_revisions import allocate_revision_number
from caliber.workspace_source import (
    assert_source_commit_digest,
    materialize_workspace_source,
)

NOW = datetime(2026, 9, 17, 12, 0, 0)


def _workspace_bundle() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            ".caliber/workspace.yaml",
            b"""apiVersion: caliber/v1alpha1
kind: Workspace
metadata:
  slug: import-journey
resources:
  workflows:
    - name: support
      path: workflows/support.json
""",
        )
        archive.writestr("workflows/support.json", b'{"name":"support"}\n')
    return output.getvalue()


def test_import_journey_materializes_allocates_retries_and_finishes(
    db_session: Session,
) -> None:
    project = CaliberProject(project_id="PRJ-import-journey", name="Import journey")
    source = CaliberWorkspaceSource(
        source_id="WSS-import-journey",
        project_id=project.project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id="repo-import-journey",
        display_path="owner/import-journey",
    )
    db_session.add_all([project, source])
    db_session.flush()

    manifest = parse_workspace_manifest(
        {
            "apiVersion": "caliber/v1alpha1",
            "kind": "Workspace",
            "metadata": {"slug": "import-journey"},
            "resources": {"workflows": [{"name": "support", "path": "workflows/support.json"}]},
        }
    )
    materialized = materialize_workspace_source(_workspace_bundle())
    revision_number = allocate_revision_number(db_session, project.project_id)
    revision = CaliberWorkspaceRevision(
        revision_id="WSR-import-journey",
        project_id=project.project_id,
        revision_number=revision_number,
        source_id=source.source_id,
        source_commit_sha="commit-import-journey",
        manifest=manifest.model_dump(mode="json"),
        manifest_sha256=manifest_digest(manifest),
        source_bundle_sha256=materialized.source_bundle_sha256,
        revision_sha256=hashlib.sha256(
            (manifest_digest(manifest) + materialized.source_bundle_sha256).encode()
        ).hexdigest(),
        created_by="@importer",
    )
    job = CaliberWorkspaceImportJob(
        import_job_id="WSI-import-journey",
        project_id=project.project_id,
        source_id=source.source_id,
        repository="owner/import-journey",
        commit_sha="commit-import-journey",
        upload_sha256=materialized.upload_sha256,
        source_bundle_sha256=materialized.source_bundle_sha256,
        manifest_sha256=manifest_digest(manifest),
        idempotency_key="import-journey-1",
        created_by="@importer",
    )
    db_session.add_all([revision, job])
    db_session.commit()

    assert (
        assert_source_commit_digest(
            db_session,
            source.source_id,
            revision.source_commit_sha,
            materialized.source_bundle_sha256,
        )
        == revision.revision_id
    )

    first_claim = claim_next_import_job(
        db_session, worker_id="import-worker-1", now=NOW, lease_seconds=60
    )
    assert first_claim is not None
    assert first_claim.attempt_count == 1
    failed = finish_import_job(
        db_session,
        job.import_job_id,
        worker_id="import-worker-1",
        status="failed",
        error_code="provider_timeout",
        error_summary="provider was temporarily unavailable",
        now=NOW + timedelta(seconds=5),
    )
    assert failed.status == "failed"

    queued = retry_failed_import_job(
        db_session,
        job.import_job_id,
        actor="@operator",
        now=NOW + timedelta(seconds=6),
    )
    assert queued.status == "queued"
    assert queued.error_code == "provider_timeout"
    assert queued.attempt_count == 1

    second_claim = claim_next_import_job(
        db_session, worker_id="import-worker-2", now=NOW + timedelta(seconds=7), lease_seconds=60
    )
    assert second_claim is not None
    assert second_claim.attempt_count == 2
    succeeded = finish_import_job(
        db_session,
        job.import_job_id,
        worker_id="import-worker-2",
        status="succeeded",
        revision_id=revision.revision_id,
        now=NOW + timedelta(seconds=8),
    )

    assert succeeded.status == "succeeded"
    assert succeeded.revision_id == revision.revision_id
    assert succeeded.attempt_count == 2
    assert succeeded.error_code is None
