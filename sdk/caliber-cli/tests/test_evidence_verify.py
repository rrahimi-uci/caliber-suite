"""The workflow-evidence CLI verifies before writing and stays non-interactive."""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Any

import httpx
import pytest
from caliber_sdk.evidence import (
    EvidenceVerificationError,
    _canonical,
    _member_name,
    _package_digest,
    verify_archive,
)

from caliber_cli.evidence_verify import verify_file


def _archive() -> bytes:
    member_id = "run_manifest:RUN-1"
    payload = b'{"run":"RUN-1"}'
    manifest: dict[str, Any] = {
        "schema_version": "v1",
        "package_id": "ep-RUN-1",
        "workflow_run_id": "RUN-1",
        "tenant_id": "tenant-local",
        "region": "local",
        "project_id": "PRJ-1",
        "view": {"authorization": "authorized", "redaction": "none"},
        "members": [
            {
                "member_id": member_id,
                "kind": "run_manifest",
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "metadata_sha256": hashlib.sha256(_canonical({"schema_version": "v1"})).hexdigest(),
                "parent_ids": [],
                "metadata": {"schema_version": "v1"},
            }
        ],
        "missing_links": [],
        "evaluator_provenance": [],
        "package_sha256": "0" * 64,
        "created_at": "2026-11-03T09:00:00Z",
    }
    manifest["package_sha256"] = _package_digest(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", _canonical(manifest))
        archive.writestr(_member_name(member_id), payload)
    return output.getvalue()


def test_workflow_evidence_verifies_before_writing(stub: Any, tmp_path: Any) -> None:
    archive = _archive()
    destination = tmp_path / "evidence.zip"
    run = stub(
        {
            "GET /workflow-runs/RUN-1/evidence": httpx.Response(200, content=archive),
        }
    )

    assert (
        run(
            [
                "--json",
                "workflow",
                "evidence",
                "RUN-1",
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    assert destination.read_bytes() == archive
    verified = verify_archive(destination.read_bytes())
    assert verified.workflow_run_id == "RUN-1"
    assert verified.origin_verified is False


def test_workflow_evidence_rejects_invalid_archive_without_creating_output(
    stub: Any, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "evidence.zip"
    run = stub(
        {
            "GET /workflow-runs/RUN-1/evidence": httpx.Response(200, content=b"not-a-zip"),
        }
    )

    assert run(["workflow", "evidence", "RUN-1", "--output", str(destination)]) == 1
    assert not destination.exists()
    assert "evidence verification failed" in capsys.readouterr().err


def test_workflow_evidence_refuses_accidental_overwrite(stub: Any, tmp_path: Any) -> None:
    destination = tmp_path / "evidence.zip"
    destination.write_bytes(b"existing")
    run = stub(
        {
            "GET /workflow-runs/RUN-1/evidence": httpx.Response(200, content=_archive()),
        }
    )

    assert run(["workflow", "evidence", "RUN-1", "--output", str(destination)]) == 1
    assert destination.read_bytes() == b"existing"


def test_verify_file_reports_a_missing_local_archive(tmp_path: Any) -> None:
    with pytest.raises(EvidenceVerificationError) as error:
        verify_file(tmp_path / "missing.zip")
    assert error.value.code == "archive_read_failed"


def test_verify_file_reads_and_verifies_a_local_archive(tmp_path: Any) -> None:
    path = tmp_path / "evidence.zip"
    path.write_bytes(_archive())
    assert verify_file(path).workflow_run_id == "RUN-1"
