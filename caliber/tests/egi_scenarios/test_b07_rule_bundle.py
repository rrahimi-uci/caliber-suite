"""B07 public path: a run uses the rule bundle it was admitted with, or is refused (B07.5b, #736).

The reference-profile app on PostgreSQL. The deployment pins a scripted rule bundle (scripted test content, not domain
acceptance). A run is admitted through the public API: admission records the bundle's path and SHA-256 in the immutable
run manifest. The worker then executes it. Every agent step's ContextManifest cites that digest and the selected rules'
provenance. A second run is admitted, then the file's bytes change (one more comment: the rules read the same): when its
agent step is assembled the bytes are verified again, the step is refused (``context_denied`` / ``rule_bundle_changed``),
the provider is never called, no context manifest is written for it, and no run manifest is touched. The first run's
manifest and every later step still cite the admitted digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from caliber.context.manifest import load_context_manifest
from caliber.context.sources import approve_source
from caliber.db.models import CaliberContextManifest, CaliberRunManifest, CaliberWorkflowRun
from caliber.knowledge.service import KnowledgeBaseService
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.workflows import runtime as runtime_module
from tests.egi_scenarios.conftest import EDITOR_A, PROJECT_A, ReferenceApp, Workspaces
from tests.egi_scenarios.test_b07_admission_sources import KB, _publish, _seed_knowledge, _start
from tests.egi_scenarios.test_v03_context_injection import _agent_manifest

RULES = Path(__file__).parent.parent / "fixtures" / "rule_bundles" / "support_rules.v1.yaml"


def _run_manifests(app: ReferenceApp) -> list[tuple[str, str, bytes]]:
    with app.session_factory() as session:
        return [
            (r.manifest_id, r.manifest_digest, bytes(r.manifest_bytes))
            for r in session.execute(
                select(CaliberRunManifest).order_by(CaliberRunManifest.manifest_id)
            ).scalars()
        ]


def _work(app: ReferenceApp) -> None:
    WorkflowRunWorker(
        session_factory=app.session_factory,
        config=app.client.app.state.config,  # type: ignore[attr-defined]
        event_bus=getattr(app.client.app.state, "event_bus", None),
    )._tick()


def _run(app: ReferenceApp, run_id: str) -> CaliberWorkflowRun:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        return run


def _context_records(app: ReferenceApp, run_id: str) -> list[dict[str, Any]]:
    with app.session_factory() as session:
        rows = session.execute(
            select(CaliberContextManifest).where(CaliberContextManifest.workflow_run_id == run_id)
        ).scalars()
        records = []
        for row in rows:
            built = load_context_manifest(session, run_id, row.step_request_id)
            assert built is not None
            records.append(built.record)
        return records


def test_a_run_cites_its_admitted_bundle_and_changed_bytes_are_refused_at_use(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if reference_app.profile != "reference":
        pytest.skip("context manifests are recorded in the reference profile")

    class Answer:
        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {"versions": [{"answer": "see policy", "retrieval_mode": "dense"}]}

    monkeypatch.setattr(KnowledgeBaseService, "query", lambda self, request, **kw: Answer())
    provider_calls: list[str] = []
    real = runtime_module._run_agent_traced

    def spy(executor: Any, agent: Any, input_text: str, **kwargs: Any) -> Any:
        provider_calls.append(input_text)
        return real(executor, agent, input_text, **kwargs)

    monkeypatch.setattr(runtime_module, "_run_agent_traced", spy)
    _seed_knowledge(reference_app)
    with reference_app.session_factory() as session:
        approve_source(session, PROJECT_A, KB, "KBV-b07-1", EDITOR_A)
        session.commit()
    workflow_id, _ = _publish(reference_app, "rules", _agent_manifest)

    bundle = tmp_path / "rules.yaml"
    bundle.write_bytes(RULES.read_bytes())
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    config = reference_app.client.app.state.config  # type: ignore[attr-defined]
    reference_app.client.app.state.config = config.model_copy(  # type: ignore[attr-defined]
        update={"rule_bundle_path": str(bundle), "rule_bundle_sha256": digest}
    )

    first = _start(reference_app, workflow_id, "rules-1")
    second = _start(reference_app, workflow_id, "rules-2")  # admitted now, run after the change
    assert first.status_code == 202 and second.status_code == 202, (first.text, second.text)
    run_ids = [first.json()["data"]["workflow_run_id"], second.json()["data"]["workflow_run_id"]]
    admitted = _run_manifests(reference_app)
    assert len(admitted) == 2

    # One run executes against the admitted bytes. (The worker claims one run per tick.)
    _work(reference_app)
    executed_id = next(i for i in run_ids if _run(reference_app, i).status == "completed")
    (record,) = _context_records(reference_app, executed_id)
    cited = record["rule_bundle"]
    assert cited["sha256"] == digest and cited["bundle_id"] == "scripted-support-rules"
    assert [rule["rule_id"] for rule in cited["rules"]] == [
        "refund-window",
        "escalate-fraud",
        "tone",
    ]
    assert len(provider_calls) == 1
    assert _run_manifests(reference_app) == admitted

    # One more comment: the rules read the same, the bytes are not the admitted bytes. The other run is refused.
    bundle.write_text(bundle.read_text(encoding="utf-8") + "# edited after admission\n")
    _work(reference_app)
    other = _run(reference_app, next(i for i in run_ids if i != executed_id))
    assert (other.status, other.error_code) == ("failed", "context_denied")
    assert "rule_bundle_changed" in (other.error_summary or "")
    assert "edited after admission" not in (other.error_summary or "")
    assert _context_records(reference_app, other.workflow_run_id) == [], (
        "no context manifest cites bytes that were not admitted"
    )
    assert len(provider_calls) == 1, "the provider was never called for the refused run"
    assert _run_manifests(reference_app) == admitted, "no admission manifest was rewritten"
