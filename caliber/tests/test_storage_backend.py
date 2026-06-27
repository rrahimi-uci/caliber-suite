"""LocalStorageBackend contract + path-safety tests (storage doc §3.7, §8.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from caliber.storage import LocalStorageBackend
from caliber.storage.base import (
    StorageConflictError,
    StorageNotFoundError,
    StoragePermissionError,
    StorageValidationError,
)


@pytest.fixture
def backend(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(f"file://{tmp_path}/ws")


def test_write_read_roundtrip_and_sha256(backend: LocalStorageBackend) -> None:
    meta = backend.write_bytes("a/b/c.txt", b"hello world", media_type="text/plain")
    assert meta.size_bytes == 11
    assert meta.sha256 is not None and len(meta.sha256) == 64
    assert meta.etag == meta.sha256
    assert backend.read_bytes("a/b/c.txt") == b"hello world"
    assert backend.exists("a/b/c.txt")
    assert not backend.exists("a/b/missing.txt")


def test_write_no_overwrite_conflicts(backend: LocalStorageBackend) -> None:
    backend.write_bytes("f.txt", b"one")
    with pytest.raises(StorageConflictError):
        backend.write_bytes("f.txt", b"two", overwrite=False)
    # overwrite=True replaces atomically
    meta = backend.write_bytes("f.txt", b"three", overwrite=True)
    assert meta.size_bytes == 5
    assert backend.read_bytes("f.txt") == b"three"


def test_byte_range_read(backend: LocalStorageBackend) -> None:
    backend.write_bytes("r.txt", b"0123456789")
    assert backend.read_bytes("r.txt", byte_range=(2, 5)) == b"2345"  # inclusive end
    with pytest.raises(StorageValidationError):
        backend.read_bytes("r.txt", byte_range=(5, 2))


def test_list_recursive(backend: LocalStorageBackend) -> None:
    backend.write_bytes("d/one.txt", b"1")
    backend.write_bytes("d/sub/two.txt", b"2")
    items, cursor = backend.list("d", recursive=True)
    keys = sorted(m.ref.key for m in items)
    assert keys == ["d/one.txt", "d/sub/two.txt"]
    assert cursor is None


def test_copy_move_delete(backend: LocalStorageBackend) -> None:
    backend.write_bytes("src.txt", b"payload")
    backend.copy("src.txt", "copy.txt")
    assert backend.read_bytes("copy.txt") == b"payload"
    backend.move("copy.txt", "moved.txt")
    assert backend.read_bytes("moved.txt") == b"payload"
    assert not backend.exists("copy.txt")
    backend.delete("moved.txt")
    assert not backend.exists("moved.txt")


def test_move_onto_self_is_noop_not_delete(backend: LocalStorageBackend) -> None:
    """Regression (#18): move(src, src) must preserve the object, not delete it
    (copy-then-delete onto the same key destroyed the only copy)."""
    backend.write_bytes("same.txt", b"keepme")
    meta = backend.move("same.txt", "same.txt", overwrite=True)
    assert backend.exists("same.txt")
    assert backend.read_bytes("same.txt") == b"keepme"
    assert meta.ref.key == "same.txt"


def test_list_does_not_hide_files_named_like_temp(backend: LocalStorageBackend) -> None:
    """Regression (#17): only true atomic-write temp stragglers (``.tmp-<pid>``)
    are hidden — a legitimate file whose name merely contains '.tmp-' must list."""
    backend.write_bytes("d/report.tmp-data.json", b"{}")  # legit file, name contains .tmp-
    backend.write_bytes("d/keep.txt", b"x")
    items, _ = backend.list("d", recursive=True)
    keys = sorted(m.ref.key for m in items)
    assert "d/report.tmp-data.json" in keys
    assert "d/keep.txt" in keys


def test_stat_and_missing(backend: LocalStorageBackend) -> None:
    backend.write_bytes("s.txt", b"abcd")
    assert backend.stat("s.txt").size_bytes == 4
    with pytest.raises(StorageNotFoundError):
        backend.stat("nope.txt")


@pytest.mark.parametrize("bad_key", ["../escape.txt", "/abs/path.txt", "a/../../b.txt"])
def test_traversal_keys_rejected(backend: LocalStorageBackend, bad_key: str) -> None:
    with pytest.raises(StorageValidationError):
        backend.write_bytes(bad_key, b"x")


def test_symlink_escape_rejected(backend: LocalStorageBackend, tmp_path: Path) -> None:
    """A symlinked directory under the root must not let reads escape (§8.2)."""
    root = tmp_path / "ws"
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "passwd").write_bytes(b"TOP SECRET")
    # Create a symlink inside the root that points outside it.
    root.mkdir(parents=True, exist_ok=True)
    link = root / "escape"
    link.symlink_to(secret)
    with pytest.raises(StoragePermissionError):
        backend.read_bytes("escape/passwd")


def test_signed_url_unsupported_on_local(backend: LocalStorageBackend) -> None:
    with pytest.raises(StorageValidationError):
        backend.signed_url("a.txt")
