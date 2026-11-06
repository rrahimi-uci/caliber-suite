"""Encrypt a bounded private incident bundle for temporary GitHub diagnostics."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .state_crypto import _atomic_write

MAGIC = b"CALIBER-AUTOCD-INCIDENT-1\n"
AAD = b"rrahimi-uci/caliber-suite:automated-cd:incident:v1"
EVENT_KINDS = (
    "qa_commands",
    "review_cycle",
    "qa_verdict",
    "handoff_gap",
    "developer_result",
    "setup_command",
    "agent_started",
    "agent_finished",
    "pr_readiness",
    "pr_check_error",
    "remote_block_error",
    "base_moved",
    "unrecorded_failure",
    "worktree_failure",
)


def _key() -> bytes:
    try:
        key = base64.b64decode(os.environ.get("AUTOCD_DIAG_KEY", ""), validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ValueError("AUTOCD_DIAG_KEY must be a base64-encoded 32-byte key")
    return key


def export(source: Path, target: Path, issue: int) -> None:
    """Select the latest attempt for one explicit issue and encrypt its evidence."""
    if type(issue) is not int or issue <= 0:
        raise ValueError("Incident issue must be a positive issue number")
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        task = db.execute(
            "SELECT * FROM tasks WHERE issue=? ORDER BY rowid DESC LIMIT 1", (issue,)
        ).fetchone()
        if task is None:
            raise ValueError("No matching task is available for incident export")
        events = db.execute(
            "SELECT kind, detail FROM events WHERE run_id=? AND issue=? "
            f"AND kind IN ({','.join('?' for _ in EVENT_KINDS)}) "
            "ORDER BY id DESC LIMIT 50",
            (task["run_id"], task["issue"], *EVENT_KINDS),
        ).fetchall()
        plaintext = json.dumps(
            {"task": dict(task), "events": [dict(row) for row in reversed(events)]},
            sort_keys=True,
        ).encode()
    nonce = os.urandom(12)
    _atomic_write(target, MAGIC + nonce + AESGCM(_key()).encrypt(nonce, plaintext, AAD))


def main() -> None:
    directory = Path(os.environ["AUTOCD_STATE_DIR"])
    raw_issue = os.environ.get("AUTOCD_INCIDENT_ISSUE", "")
    if not raw_issue.isdecimal() or int(raw_issue) <= 0:
        raise ValueError("AUTOCD_INCIDENT_ISSUE must be a positive issue number")
    export(directory / "workflow.sqlite3", directory / "incident.enc", int(raw_issue))
    print("Encrypted incident bundle created")


if __name__ == "__main__":
    main()
