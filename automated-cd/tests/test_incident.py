import base64
import json
import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from autocd.incident import AAD, MAGIC, export, main
from autocd.state import Store


def test_incident_export_is_encrypted_and_bounded(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "state"
    store = Store(directory)
    store.start("run")
    store.add_task("run", 821, "Pilot", "branch", tmp_path / "worktree")
    store.event("run", 821, "qa_commands", [{"output": "PRIVATE QA EVIDENCE"}])
    store.event("run", 821, "review_cycle", {"result": {"findings": ["PRIVATE REVIEW"]}})
    store.event("run", 821, "agent_progress", {"update": "UNRELATED STREAM"})
    store.phase("run", 821, "blocked", failure="PRIVATE DEVELOPER BLOCKER")
    store.add_task("run", 999, "Pilot", "other", tmp_path / "other-worktree")
    store.event("run", 999, "review_cycle", {"result": {"findings": ["OTHER ISSUE"]}})
    key = os.urandom(32)
    monkeypatch.setenv("AUTOCD_DIAG_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("AUTOCD_STATE_DIR", str(directory))
    monkeypatch.setenv("AUTOCD_INCIDENT_ISSUE", "821")
    main()
    assert capsys.readouterr().out == "Encrypted incident bundle created\n"
    payload = (directory / "incident.enc").read_bytes()
    assert payload.startswith(MAGIC)
    assert b"PRIVATE" not in payload
    nonce = payload[len(MAGIC) : len(MAGIC) + 12]
    decoded = json.loads(AESGCM(key).decrypt(nonce, payload[len(MAGIC) + 12 :], AAD))
    assert decoded["task"]["issue"] == 821
    assert decoded["task"]["failure"] == "PRIVATE DEVELOPER BLOCKER"
    assert [event["kind"] for event in decoded["events"]] == ["qa_commands", "review_cycle"]
    assert "OTHER ISSUE" not in json.dumps(decoded)
    with pytest.raises(InvalidTag):
        AESGCM(os.urandom(32)).decrypt(nonce, payload[len(MAGIC) + 12 :], AAD)


def test_incident_export_requires_task_and_valid_key(tmp_path, monkeypatch):
    store = Store(tmp_path)
    target = tmp_path / "incident.enc"
    monkeypatch.setenv("AUTOCD_DIAG_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(ValueError, match="No matching task"):
        export(store.path, target, 1)
    assert not target.exists()
    store.start("run")
    store.add_task("run", 1, "Pilot", "branch", tmp_path / "worktree")
    with pytest.raises(ValueError, match="No matching task"):
        export(store.path, target, 2)
    with pytest.raises(ValueError, match="positive issue"):
        export(store.path, target, 0)
    monkeypatch.setenv("AUTOCD_DIAG_KEY", "invalid")
    with pytest.raises(ValueError, match="AUTOCD_DIAG_KEY"):
        export(store.path, target, 1)
    assert not target.exists()
    monkeypatch.setenv("AUTOCD_DIAG_KEY", base64.b64encode(b"too short").decode())
    with pytest.raises(ValueError, match="AUTOCD_DIAG_KEY"):
        export(store.path, target, 1)
    assert not target.exists()


def test_incident_export_includes_private_pr_monitor_errors(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.start("run")
    store.add_task("run", 42, "Pilot", "branch", tmp_path / "worktree")
    store.phase("run", 42, "ci_wait")
    store.event("run", 42, "pr_check_error", "PRIVATE PR ERROR")
    store.event("run", 42, "pr_readiness", {"reasons": ["PRIVATE REVIEW GATE"]})
    key = os.urandom(32)
    monkeypatch.setenv("AUTOCD_DIAG_KEY", base64.b64encode(key).decode())
    target = tmp_path / "incident.enc"
    export(store.path, target, 42)
    payload = target.read_bytes()
    assert b"PRIVATE" not in payload
    nonce = payload[len(MAGIC) : len(MAGIC) + 12]
    decoded = json.loads(AESGCM(key).decrypt(nonce, payload[len(MAGIC) + 12 :], AAD))
    assert [event["kind"] for event in decoded["events"]] == ["pr_check_error", "pr_readiness"]
    assert decoded["task"]["failure"] is None
    monkeypatch.setenv("AUTOCD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOCD_INCIDENT_ISSUE", "invalid")
    with pytest.raises(ValueError, match="positive issue"):
        main()
