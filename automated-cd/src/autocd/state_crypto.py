"""Authenticated encryption for coordinator state stored in public Actions artifacts."""

from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"CALIBER-AUTOCD-STATE-1\n"
AAD = b"rrahimi-uci/caliber-suite:automated-cd:state:v1"


def _key() -> bytes:
    encoded = os.environ.get("AUTOCD_STATE_KEY", "").strip()
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError("AUTOCD_STATE_KEY must be base64 for a 32-byte key") from error
    if len(key) != 32:
        raise ValueError("AUTOCD_STATE_KEY must be base64 for a 32-byte key")
    return key


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, target)


def seal(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    # SQLite may leave committed transactions in -wal after the coordinator is
    # terminated. A backup reads the consistent main database plus its WAL.
    with tempfile.TemporaryDirectory(dir=source.parent) as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        with (
            closing(sqlite3.connect(source)) as original,
            closing(sqlite3.connect(snapshot)) as copy,
        ):
            original.backup(copy)
        plaintext = snapshot.read_bytes()
    nonce = os.urandom(12)
    payload = MAGIC + nonce + AESGCM(_key()).encrypt(nonce, plaintext, AAD)
    _atomic_write(target, payload)


def open_state(source: Path, target: Path) -> None:
    payload = source.read_bytes()
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 12 + 16:
        raise ValueError("Invalid encrypted coordinator state")
    nonce = payload[len(MAGIC) : len(MAGIC) + 12]
    plaintext = AESGCM(_key()).decrypt(nonce, payload[len(MAGIC) + 12 :], AAD)
    _atomic_write(target, plaintext)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seal", "open"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.action == "seal":
        seal(args.source, args.target)
    else:
        open_state(args.source, args.target)


if __name__ == "__main__":
    main()
