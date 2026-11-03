"""B16.4 coordinated restore smoke: restore pins, preserve an unknown, reconcile once."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from caliber.db.models import (
    CaliberProject,
    CaliberRunManifest,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.effects.records import proposal_state
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy
from tests.egi_scenarios.conftest import Downstream
from tests.egi_scenarios.test_v07_restart_exactly_once import (
    PROJECT,
    _approved_proposal,
    _assert_receipt_matches_the_downstream,
    _kill,
    _new_owner,
    _reconcile,
    _start_child,
    _wait_for,
)
from tests.test_event_outbox import _database


@pytest.fixture(autouse=True)
def _loopback_allowed() -> Iterator[None]:
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield
    bind_egress_policy(None)


def test_restore_mid_scenario_completes_once(tmp_path: Path, monkeypatch) -> None:  # noqa: PLR0915
    """A restored admitted run keeps its pins and settles one unknown external write.

    The SQLite database is the backup artifact in this offline smoke. The downstream is
    intentionally left running: its one committed mutation is the external state that the
    restored CALIBER database must reconcile by authoritative lookup, never by resending.
    """
    with _database("sqlite", tmp_path, monkeypatch) as (engine, factory):
        manifest_record = {
            "schema_version": "v1",
            "workflow_run_id": "RUN-ep",
            "pins": {
                "workflow_version": "wf-ep:v1",
                "bundle_digest": "sha256:" + "a" * 64,
                "policy_version": "policy-v1",
            },
            "artifacts": [
                {"artifact_id": "artifact-restore-1", "sha256": "b" * 64},
            ],
        }
        manifest_bytes = json.dumps(manifest_record, sort_keys=True, separators=(",", ":")).encode()
        with factory() as session:
            session.add(CaliberProject(project_id=PROJECT, name="Restore smoke", owner="@test"))
            session.add(
                CaliberWorkflow(
                    workflow_id="wf-ep", project_id=PROJECT, name="Restore smoke", owner="@test"
                )
            )
            session.add(
                CaliberWorkflowRun(
                    workflow_run_id="RUN-ep",
                    workflow_id="wf-ep",
                    project_id=PROJECT,
                    status="running",
                    manifest_legacy=False,
                    summary={
                        "pins": manifest_record["pins"],
                        "artifact_id": "artifact-restore-1",
                    },
                )
            )
            session.commit()
            session.add(
                CaliberRunManifest(
                    manifest_id="manifest-restore-1",
                    workflow_run_id="RUN-ep",
                    schema_version="v1",
                    manifest_bytes=manifest_bytes,
                    manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
                )
            )
            session.commit()

        proposal_id = _approved_proposal(factory)
        downstream = Downstream(tmp_path / "downstream")
        downstream.start()
        child = None
        try:
            downstream.set_fault("delay_response_seconds", seconds=20)
            marker = tmp_path / "dispatch-marker"
            child = _start_child(engine, downstream, proposal_id, "after_response", marker)
            _wait_for(lambda: downstream.mutations() == 1, child, "the external write")
            _kill(child)
            child = None

            with factory() as session:
                assert proposal_state(session, proposal_id).state == "dispatched"
                backup_manifest = session.get(CaliberRunManifest, "manifest-restore-1")
                assert backup_manifest is not None
                expected_manifest = bytes(backup_manifest.manifest_bytes)
                expected_digest = backup_manifest.manifest_digest

            database_path = Path(str(engine.url.database))
            backup_path = tmp_path / "caliber.backup.db"
            engine.dispose()
            shutil.copy2(database_path, backup_path)

            # Simulate a damaged target after the backup. Restoring the exact artifact must
            # bring back both the run pins and the unresolved proposal state.
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE caliber_workflow_runs SET summary = ? WHERE workflow_run_id = ?",
                    (json.dumps({"tampered": True}), "RUN-ep"),
                )
                connection.commit()
            shutil.copy2(backup_path, database_path)
            engine.dispose()

            with factory() as session:
                run = session.get(CaliberWorkflowRun, "RUN-ep")
                restored_manifest = session.get(CaliberRunManifest, "manifest-restore-1")
                assert run is not None and run.summary == {
                    "pins": manifest_record["pins"],
                    "artifact_id": "artifact-restore-1",
                }
                assert restored_manifest is not None
                assert (
                    bytes(restored_manifest.manifest_bytes),
                    restored_manifest.manifest_digest,
                ) == (expected_manifest, expected_digest)
                assert proposal_state(session, proposal_id).state == "dispatched"

            results = _reconcile(factory, _new_owner(factory), downstream)
            assert [(item.proposal_id, item.state) for item in results] == [
                (proposal_id, "committed")
            ]
            assert downstream.mutations() == 1, "restore reconciliation must not resend the write"
            _assert_receipt_matches_the_downstream(factory, proposal_id, downstream)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            downstream.close()
