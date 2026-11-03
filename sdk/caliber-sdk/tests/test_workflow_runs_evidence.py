"""Evidence archive download parity and server-free verification."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import stat
import warnings
import zipfile
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient, EvidenceVerificationError, verify_archive
from caliber_sdk import evidence as evidence_module
from caliber_sdk.aio import AsyncCaliberClient
from caliber_sdk.evidence import VerificationLimits, _canonical, _member_name, _package_digest

BASE = "https://caliber.test"


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


def _manifest() -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(ARCHIVE)) as archive:
        return json.loads(archive.read("manifest.json"))


def _archive_for(
    manifest: dict[str, Any],
    *,
    payload: bytes = b'{"run":"RUN-1"}',
    manifest_bytes: bytes | None = None,
    extra_entries: tuple[str, ...] = (),
    duplicate_manifest: bool = False,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    member_id = manifest["members"][0]["member_id"]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        data = manifest_bytes or _canonical(manifest)
        archive.writestr("manifest.json", data)
        if duplicate_manifest:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("manifest.json", data)
        archive.writestr(_member_name(member_id), payload)
        for name in extra_entries:
            archive.writestr(name, b"extra")
    return output.getvalue()


ARCHIVE = _archive()


def _sync_client(handler: Any) -> CaliberClient:
    return CaliberClient(
        BASE,
        token="calpat_test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_sync_evidence_download_uses_the_raw_archive_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/workflow-runs/RUN-1/evidence")
        return httpx.Response(200, content=ARCHIVE, headers={"content-type": "application/zip"})

    with _sync_client(handler) as client:
        downloaded = client.workflows.runs.evidence("RUN-1")

    assert downloaded == ARCHIVE
    assert verify_archive(downloaded).workflow_run_id == "RUN-1"


def test_async_evidence_download_matches_the_sync_surface() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/workflow-runs/RUN-1/evidence")
        return httpx.Response(200, content=ARCHIVE)

    async def main() -> bytes:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with AsyncCaliberClient(BASE, token="calpat_test", http_client=http) as client:
            return await client.workflows.evidence("RUN-1")

    assert asyncio.run(main()) == ARCHIVE


def test_verifier_rejects_tampered_payload_and_unsafe_member() -> None:
    assert verify_archive(ARCHIVE).origin_verified is False
    tampered = ARCHIVE.replace(b'{"run":"RUN-1"}', b'{"run":"RUN-2"}', 1)
    with pytest.raises(EvidenceVerificationError) as error:
        verify_archive(tampered)
    assert error.value.code in {"member_read_failed", "payload_digest_mismatch"}

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../manifest.json", b"{}")
    with pytest.raises(EvidenceVerificationError) as error:
        verify_archive(unsafe.getvalue())
    assert error.value.code == "unsafe_member_path"


@pytest.mark.parametrize(
    "mutation, code",
    [
        (lambda manifest: [], "invalid_manifest_shape"),
        (lambda manifest: manifest.update({"extra": True}), "invalid_manifest"),
        (lambda manifest: manifest.update({"schema_version": "v2"}), "unsupported_schema"),
        (lambda manifest: manifest.update({"package_id": ""}), "invalid_manifest"),
        (lambda manifest: manifest.update({"view": {}}), "invalid_view"),
        (
            lambda manifest: manifest["view"].update({"redaction": "secret"}),
            "invalid_view",
        ),
        (lambda manifest: manifest.update({"members": []}), "invalid_members"),
        (
            lambda manifest: manifest["members"][0].update({"extra": True}),
            "invalid_member",
        ),
        (
            lambda manifest: manifest["members"][0].update({"member_id": ""}),
            "invalid_member",
        ),
        (
            lambda manifest: manifest["members"].append(copy.deepcopy(manifest["members"][0])),
            "duplicate_member",
        ),
        (
            lambda manifest: manifest["members"][0].update({"kind": "Run_manifest"}),
            "invalid_member",
        ),
        (
            lambda manifest: manifest["members"][0].update({"kind": "unknown"}),
            "unsupported_member_schema",
        ),
        (
            lambda manifest: manifest["members"][0].update({"payload_sha256": "bad"}),
            "invalid_member",
        ),
        (
            lambda manifest: manifest["members"][0].update({"parent_ids": [None]}),
            "invalid_parent_links",
        ),
        (
            lambda manifest: manifest["members"][0].update({"parent_ids": ["p", "p"]}),
            "invalid_parent_links",
        ),
        (
            lambda manifest: manifest["members"][0]["metadata"].update({"schema_version": "v2"}),
            "unsupported_member_schema",
        ),
        (lambda manifest: manifest.update({"missing_links": None}), "invalid_missing_links"),
        (
            lambda manifest: manifest["members"][0].update({"parent_ids": ["missing"]}),
            "broken_parent_link",
        ),
        (
            lambda manifest: manifest.update({"evaluator_provenance": None}),
            "invalid_provenance",
        ),
        (
            lambda manifest: manifest.update({"package_sha256": "bad"}),
            "invalid_manifest",
        ),
        (
            lambda manifest: manifest.update({"package_sha256": "0" * 64}),
            "package_digest_mismatch",
        ),
    ],
)
def test_verifier_manifest_refusals(mutation: Any, code: str) -> None:
    manifest = _manifest()
    result = mutation(manifest)
    if isinstance(result, list):
        manifest = result
    with pytest.raises(EvidenceVerificationError) as error:
        evidence_module._validate_manifest(manifest)
    assert error.value.code == code


@pytest.mark.parametrize(
    "name, file_size, compress_size, flags, method, external_attr, code",
    [
        ("", 1, 1, 0, zipfile.ZIP_STORED, 0, "unsafe_member_path"),
        ("/absolute", 1, 1, 0, zipfile.ZIP_STORED, 0, "unsafe_member_path"),
        ("folder", 1, 1, 0, zipfile.ZIP_STORED, 0, "unexpected_directory"),
        ("member", 1, 1, 0, zipfile.ZIP_STORED, stat.S_IFLNK | 0o777, "symlink_member"),
        ("member", 1, 1, 1, zipfile.ZIP_STORED, 0, "encrypted_member"),
        ("member", 1, 1, 0, 99, 0, "unsupported_compression"),
        ("member", 100, 1, 0, zipfile.ZIP_STORED, 0, "compression_ratio_limit"),
    ],
)
def test_verifier_entry_and_limit_refusals(
    name: str,
    file_size: int,
    compress_size: int,
    flags: int,
    method: int,
    external_attr: int,
    code: str,
) -> None:
    if code == "unexpected_directory":
        info = type(
            "DirectoryInfo",
            (),
            {
                "filename": name,
                "file_size": file_size,
                "compress_size": compress_size,
                "flag_bits": flags,
                "compress_type": method,
                "external_attr": external_attr << 16,
                "is_dir": lambda self: True,
            },
        )()
    else:
        info = zipfile.ZipInfo(name)
        info.file_size = file_size
        info.compress_size = compress_size
        info.flag_bits = flags
        info.compress_type = method
        info.external_attr = external_attr << 16
    with pytest.raises(EvidenceVerificationError) as error:
        evidence_module._check_entry(info, VerificationLimits(max_compression_ratio=10))
    assert error.value.code == code

    with pytest.raises(EvidenceVerificationError, match="limits"):
        evidence_module._check_limits(VerificationLimits(max_archive_bytes=0))
    with pytest.raises(EvidenceVerificationError, match="member exceeds"):
        size_limited = zipfile.ZipInfo("member")
        size_limited.file_size = 10
        size_limited.compress_size = 10
        evidence_module._check_entry(size_limited, VerificationLimits(max_member_bytes=1))


def test_verifier_rejects_noncanonical_values() -> None:
    with pytest.raises(EvidenceVerificationError, match="canonical JSON"):
        evidence_module._canonical({object(): "not-json"})


def test_verifier_rejects_archive_structure_and_payload_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    assert verify_archive(ARCHIVE).valid
    with pytest.raises(EvidenceVerificationError, match="bytes"):
        verify_archive("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(EvidenceVerificationError, match="archive exceeds"):
        verify_archive(ARCHIVE, limits=VerificationLimits(max_archive_bytes=len(ARCHIVE) - 1))
    with pytest.raises(EvidenceVerificationError, match="valid ZIP"):
        verify_archive(b"not-a-zip")
    with pytest.raises(EvidenceVerificationError, match="too many"):
        verify_archive(ARCHIVE, limits=VerificationLimits(max_entries=1))
    with pytest.raises(EvidenceVerificationError, match="duplicate"):
        verify_archive(_archive_for(manifest, duplicate_manifest=True))
    with pytest.raises(EvidenceVerificationError, match="total uncompressed"):
        verify_archive(ARCHIVE, limits=VerificationLimits(max_total_uncompressed_bytes=1))
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(EvidenceVerificationError, match="no manifest"):
        verify_archive(empty.getvalue())
    with pytest.raises(EvidenceVerificationError, match="valid JSON"):
        verify_archive(_archive_for(manifest, manifest_bytes=b"not-json"))
    with pytest.raises(EvidenceVerificationError, match="canonical"):
        verify_archive(_archive_for(manifest, manifest_bytes=json.dumps(manifest).encode()))
    with pytest.raises(EvidenceVerificationError, match="match the manifest"):
        verify_archive(_archive_for(manifest, extra_entries=("members/extra.json",)))

    metadata_manifest = copy.deepcopy(manifest)
    metadata_manifest["members"][0]["metadata"]["tampered"] = True
    metadata_manifest["package_sha256"] = _package_digest(metadata_manifest)
    with pytest.raises(EvidenceVerificationError, match="metadata"):
        verify_archive(_archive_for(metadata_manifest))

    with pytest.raises(EvidenceVerificationError, match="payload"):
        verify_archive(_archive_for(manifest, payload=b"tampered"))

    read_failure_archive = _archive_for(manifest, compression=zipfile.ZIP_DEFLATED)
    with (
        pytest.raises(EvidenceVerificationError, match="could not be read"),
        monkeypatch.context() as patch,
    ):
        patch.setattr(
            zipfile.ZipFile,
            "open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
        )
        verify_archive(read_failure_archive)
