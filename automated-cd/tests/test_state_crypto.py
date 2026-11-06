from __future__ import annotations

import base64
import sqlite3
import sys

import pytest
from cryptography.exceptions import InvalidTag

from autocd.state_crypto import main, open_state, seal


def test_state_round_trip_is_encrypted_and_rejects_tampering(tmp_path, monkeypatch):
    key = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setenv("AUTOCD_STATE_KEY", key)
    source = tmp_path / "workflow.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE events (detail TEXT)")
        connection.execute("INSERT INTO events VALUES ('private audit content')")
    encrypted = tmp_path / "workflow.state.enc"
    restored = tmp_path / "restored.sqlite3"
    seal(source, encrypted)
    assert b"private audit content" not in encrypted.read_bytes()
    assert encrypted.stat().st_mode & 0o777 == 0o600
    open_state(encrypted, restored)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT detail FROM events").fetchall() == [
            ("private audit content",)
        ]
    assert restored.stat().st_mode & 0o777 == 0o600

    encrypted.write_bytes(encrypted.read_bytes()[:-1] + b"X")
    restored.unlink()
    with pytest.raises(InvalidTag):
        open_state(encrypted, restored)
    assert not restored.exists()


def test_state_rejects_wrong_key_and_plaintext(tmp_path, monkeypatch):
    source = tmp_path / "state"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE events (detail TEXT)")
        connection.execute("INSERT INTO events VALUES ('secret')")
    target = tmp_path / "encrypted"
    monkeypatch.setenv("AUTOCD_STATE_KEY", base64.b64encode(b"a" * 32).decode())
    seal(source, target)
    monkeypatch.setenv("AUTOCD_STATE_KEY", base64.b64encode(b"b" * 32).decode())
    with pytest.raises(InvalidTag):
        open_state(target, tmp_path / "output")
    with pytest.raises(ValueError, match="Invalid encrypted"):
        open_state(source, tmp_path / "output")
    monkeypatch.setenv("AUTOCD_STATE_KEY", "invalid")
    with pytest.raises(ValueError, match="32-byte key"):
        seal(source, tmp_path / "output")
    monkeypatch.setenv("AUTOCD_STATE_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="32-byte key"):
        seal(source, tmp_path / "output")
    with pytest.raises(FileNotFoundError):
        seal(tmp_path / "missing", tmp_path / "output")


def test_state_snapshot_includes_committed_uncheckpointed_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCD_STATE_KEY", base64.b64encode(b"k" * 32).decode())
    source = tmp_path / "workflow.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE tasks (issue INTEGER)")
    connection.execute("INSERT INTO tasks VALUES (821)")
    connection.commit()
    assert (tmp_path / "workflow.sqlite3-wal").stat().st_size > 0
    encrypted = tmp_path / "workflow.state.enc"
    restored = tmp_path / "restored.sqlite3"
    seal(source, encrypted)
    open_state(encrypted, restored)
    with sqlite3.connect(restored) as recovered:
        assert recovered.execute("SELECT issue FROM tasks").fetchall() == [(821,)]
    connection.close()


def test_state_cli_and_atomic_write_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCD_STATE_KEY", base64.b64encode(b"k" * 32).decode())
    source = tmp_path / "state.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE events (detail TEXT)")
    encrypted = tmp_path / "state.enc"
    restored = tmp_path / "restored.sqlite3"
    monkeypatch.setattr(sys, "argv", ["state_crypto", "seal", str(source), str(encrypted)])
    main()
    monkeypatch.setattr(sys, "argv", ["state_crypto", "open", str(encrypted), str(restored)])
    main()
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (0,)

    def fail_sync(_fd):
        raise OSError("disk failure")

    monkeypatch.setattr("autocd.state_crypto.os.fsync", fail_sync)
    with pytest.raises(OSError, match="disk failure"):
        seal(source, tmp_path / "failed.enc")
    assert not (tmp_path / "failed.enc").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "restored.sqlite3",
        "state.enc",
        "state.sqlite3",
    ]
