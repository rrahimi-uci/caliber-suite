"""P4-B tests for deterministic Workspace source materialization."""

from __future__ import annotations

import stat
import tarfile
import zipfile
from io import BytesIO

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProject, CaliberWorkspaceRevision
from caliber.storage import StorageValidationError
from caliber.workspace_source import (
    WorkspaceSourceDigestConflictError,
    assert_source_commit_digest,
    materialize_workspace_source,
)


def _zip_bytes(
    entries: list[tuple[str, bytes, bool]],
    *,
    order: list[int] | None = None,
    compresslevel: int = 6,
    date_time: tuple[int, int, int, int, int, int] = (2024, 1, 1, 0, 0, 0),
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compresslevel
    ) as archive:
        for index in order if order is not None else range(len(entries)):
            path, content, executable = entries[index]
            info = zipfile.ZipInfo(path, date_time=date_time)
            info.create_system = 3
            info.external_attr = ((0o755 if executable else 0o644) << 16) | (
                0o111 if executable else 0
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def _symlink_zip() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"target")
    return output.getvalue()


def _revision(
    project_id: str,
    *,
    source_id: str = "WSS-source",
    commit_sha: str = "commit-1",
    digest: str = "a" * 64,
) -> CaliberWorkspaceRevision:
    return CaliberWorkspaceRevision(
        revision_id="WSR-source-observation",
        project_id=project_id,
        revision_number=1,
        source_id=source_id,
        source_commit_sha=commit_sha,
        manifest={"apiVersion": "caliber/v1alpha1"},
        manifest_sha256="b" * 64,
        source_bundle_sha256=digest,
        revision_sha256="c" * 64,
    )


def test_canonical_digest_ignores_zip_order_compression_and_timestamps() -> None:
    entries = [
        ("README.md", b"# Workspace\n", False),
        ("scripts/run.py", b"print('ok')\n", True),
    ]
    first = materialize_workspace_source(_zip_bytes(entries, compresslevel=1))
    second = materialize_workspace_source(
        _zip_bytes(entries, order=[1, 0], compresslevel=9, date_time=(2025, 6, 7, 8, 9, 10))
    )

    assert first.upload_sha256 != second.upload_sha256
    assert first.source_bundle_sha256 == second.source_bundle_sha256
    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.snapshot_bytes == second.snapshot_bytes
    assert [entry.path for entry in first.entries] == ["README.md", "scripts/run.py"]
    assert first.entries[1].executable is True
    assert first.entries[0].media_type == "text/markdown"


def test_canonical_digest_changes_when_content_or_executable_bit_changes() -> None:
    original = materialize_workspace_source(_zip_bytes([("run.py", b"print(1)\n", False)]))
    changed_content = materialize_workspace_source(_zip_bytes([("run.py", b"print(2)\n", False)]))
    changed_mode = materialize_workspace_source(_zip_bytes([("run.py", b"print(1)\n", True)]))

    assert original.source_bundle_sha256 != changed_content.source_bundle_sha256
    assert original.source_bundle_sha256 != changed_mode.source_bundle_sha256


def test_snapshot_is_a_fixed_tar_of_sorted_files() -> None:
    materialized = materialize_workspace_source(
        _zip_bytes([("z.txt", b"z", False), ("a.txt", b"a", True)])
    )

    with tarfile.open(fileobj=BytesIO(materialized.snapshot_bytes), mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ["a.txt", "z.txt"]
        assert members[0].mode == 0o755
        assert members[0].mtime == 0
        assert archive.extractfile(members[1]).read() == b"z"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_bundle_bytes": 1}, "source bundle is"),
        ({"max_entries": 1}, "entries"),
        ({"max_file_bytes": 2}, "max 2"),
        ({"max_total_bytes": 2}, "zip bomb"),
        ({"max_ratio": 1}, "compression ratio"),
    ],
)
def test_archive_limits_fail_closed(kwargs: dict[str, int], match: str) -> None:
    data = _zip_bytes([("large.txt", b"A" * 1000, False), ("other.txt", b"x", False)])
    with pytest.raises(StorageValidationError, match=match):
        materialize_workspace_source(data, **kwargs)


def test_duplicate_normalized_paths_and_symlinks_are_rejected() -> None:
    with pytest.raises(StorageValidationError, match="unsafe archive entry"):
        materialize_workspace_source(_zip_bytes([("../escape.txt", b"x", False)]))

    duplicate = _zip_bytes([("a/./file.txt", b"one", False), ("a/file.txt", b"two", False)])
    with pytest.raises(StorageValidationError, match="duplicate normalized path"):
        materialize_workspace_source(duplicate)

    with pytest.raises(StorageValidationError, match="symlink"):
        materialize_workspace_source(_symlink_zip())


def test_source_commit_observation_is_idempotent_and_rejects_equivocation(
    db_session: Session,
) -> None:
    project = CaliberProject(project_id="PRJ-source-observation", name="Source observation")
    db_session.add(project)
    db_session.flush()
    revision = _revision(project.project_id)
    db_session.add(revision)
    db_session.commit()

    assert (
        assert_source_commit_digest(
            db_session, "WSS-source", "commit-1", revision.source_bundle_sha256
        )
        == revision.revision_id
    )
    with pytest.raises(WorkspaceSourceDigestConflictError, match="source_commit_digest_conflict"):
        assert_source_commit_digest(db_session, "WSS-source", "commit-1", "d" * 64)
    assert assert_source_commit_digest(db_session, "WSS-source", "new-commit", "d" * 64) is None


def test_source_commit_observation_requires_all_identity_fields(db_session: Session) -> None:
    with pytest.raises(ValueError, match="required"):
        assert_source_commit_digest(db_session, "", "commit-1", "a" * 64)
