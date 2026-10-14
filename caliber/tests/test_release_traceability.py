"""One public hop from a live version reaches its evidence (alpha gap P1-14).

"Why is v3 live?" had no answer through the API: the registry version carried only an
approval id and an artifact type, the release operation's evidence had no job, ``GET
/jobs/{id}`` named no approval or applier, and ``eval_results`` did not say which dataset
version the candidate was judged on. These tests apply a candidate through the real route
with the real ``MLflowPromoter`` over a recording MLflow stand-in, then walk from the version
to the job to its evaluation using only public calls.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.apply import release_provenance
from caliber.db.models import CaliberRefinementJob, CaliberReleaseOperation
from caliber.promoter import build_promoter
from caliber.routes import prompts as prompt_routes
from tests.test_routes_jobs_apply import APPLY_PATH, _seed_candidate_ready_job

JOBS = "/ajax-api/2.0/mlflow/caliber/jobs"
VERSIONS = "/ajax-api/2.0/mlflow/caliber/prompts/support-agent/versions"

EVAL_RESULTS: dict[str, Any] = {
    "gate": {"passed": True},
    "candidate": {"overall": 0.94, "dimensions": {"Correctness": 0.94}},
    "eval_dataset_id": "ED-support",
    "eval_dataset_version": 3,
    "n_examples": 3,
    "provider": "fake",
    "simulated": True,
}


class _Registry:
    """A recording MLflow stand-in: every registered version keeps its tags."""

    def __init__(self) -> None:
        self.versions: dict[int, dict[str, Any]] = {}
        self.alias: dict[str, int] = {"prod": 1}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = self

        def register_prompt(**kwargs: Any) -> object:
            number = max(registry.versions, default=1) + 1
            registry.versions[number] = dict(kwargs)
            return SimpleNamespace(version=number, uri=f"prompts:/{kwargs['name']}/{number}")

        def load_prompt(_ref: str, **_kwargs: Any) -> object:
            return SimpleNamespace(version=registry.alias["prod"])

        def set_prompt_alias(*, name: str, alias: str, version: int) -> None:
            registry.alias[alias] = version

        stub = types.ModuleType("mlflow")
        genai = types.ModuleType("mlflow.genai")
        genai.register_prompt = register_prompt  # type: ignore[attr-defined]
        genai.load_prompt = load_prompt  # type: ignore[attr-defined]
        genai.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]
        stub.genai = genai  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mlflow", stub)
        monkeypatch.setitem(sys.modules, "mlflow.genai", genai)

    def client(self) -> object:
        registry = self

        class _Client:
            def search_prompt_versions(self, _name: str) -> list[object]:
                return [
                    SimpleNamespace(
                        version=number,
                        source=f"prompts:/support-agent/{number}",
                        tags=dict(call.get("tags") or {}),
                        description=None,
                        commit_message=call.get("commit_message"),
                        creation_timestamp=number,
                        last_updated_timestamp=number,
                        run_id=None,
                        aliases=[],
                    )
                    for number, call in registry.versions.items()
                ]

            def get_prompt_version_by_alias(self, _name: str, alias: str) -> object:
                if alias not in registry.alias:
                    raise RuntimeError("no such alias")
                return SimpleNamespace(version=registry.alias[alias])

        return _Client()


@pytest.fixture
def registry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> _Registry:
    recording = _Registry()
    recording.install(monkeypatch)
    client.app.state.promoter = build_promoter("mlflow")  # type: ignore[attr-defined]
    monkeypatch.setattr(prompt_routes, "_build_mlflow_client", recording.client)
    return recording


def _seed_job_with_evidence(db_session: Session) -> None:
    _seed_candidate_ready_job(db_session)
    job = db_session.get(CaliberRefinementJob, "RFN-A")
    assert job is not None
    job.eval_results = dict(EVAL_RESULTS)
    db_session.commit()


# --- provenance ---------------------------------------------------------------------------


def test_release_provenance_names_the_job_dataset_version_and_provider() -> None:
    job = CaliberRefinementJob(job_id="RFN-9", eval_results=dict(EVAL_RESULTS))

    assert release_provenance(job) == {
        "job_id": "RFN-9",
        "eval_dataset_id": "ED-support",
        "eval_dataset_version": "3",
        "eval_provider": "fake",
        "eval_simulated": "true",
    }


def test_release_provenance_skips_what_is_unknown() -> None:
    assert release_provenance(CaliberRefinementJob(job_id="RFN-1", eval_results=None)) == {
        "job_id": "RFN-1"
    }
    bare = CaliberRefinementJob(job_id="RFN-2", eval_results={"eval_dataset_version": None})
    assert release_provenance(bare) == {"job_id": "RFN-2"}


def test_release_provenance_keeps_a_measured_evaluation_distinct_from_a_simulated_one() -> None:
    job = CaliberRefinementJob(
        job_id="RFN-3", eval_results={"provider": "mlflow", "simulated": False}
    )
    assert release_provenance(job)["eval_simulated"] == "false"


# --- Apply writes it ------------------------------------------------------------------------


def test_apply_tags_the_registered_version_and_records_the_evidence(
    client: TestClient, db_session: Session, registry: _Registry
) -> None:
    _seed_job_with_evidence(db_session)

    response = client.post(APPLY_PATH.format(job_id="RFN-A"))

    assert response.status_code == 200, response.text
    tags = registry.versions[2]["tags"]
    assert tags["caliber.job_id"] == "RFN-A"
    assert tags["caliber.eval_dataset_id"] == "ED-support"
    assert tags["caliber.eval_dataset_version"] == "3"
    assert tags["caliber.eval_provider"] == "fake"
    assert tags["caliber.eval_simulated"] == "true"
    assert tags["caliber.approval_id"].startswith("AP-")
    db_session.expire_all()
    operation = db_session.query(CaliberReleaseOperation).one()
    assert operation.evidence["job_id"] == "RFN-A"
    assert operation.evidence["eval_dataset_version"] == "3"
    assert operation.evidence["eval_provider"] == "fake"


# --- the walk -------------------------------------------------------------------------------


def test_a_live_version_walks_to_its_job_evaluation_and_dataset_version(
    client: TestClient, db_session: Session, registry: _Registry
) -> None:
    _seed_job_with_evidence(db_session)
    assert client.post(APPLY_PATH.format(job_id="RFN-A")).status_code == 200

    # Hop 1: the version list names the evidence for the version that went live.
    versions = client.get(VERSIONS).json()["data"]
    live = next(v for v in versions if v["current"])
    assert live["version"] == 2
    provenance = live["provenance"]
    assert provenance["job_id"] == "RFN-A"

    # Hop 2: the job, from the id the version carried, answers who and which version.
    job = client.get(f"{JOBS}/{provenance['job_id']}").json()["data"]
    assert job["status"] == "applied"
    assert job["applied_version"] == live["version"]
    assert job["applied_by"]
    assert job["approval_id"] == provenance["approval_id"]

    # Hop 3: the job's evaluation names the dataset and the version it was judged on.
    results = job["eval_results"]
    assert results["eval_dataset_id"] == provenance["eval_dataset_id"] == "ED-support"
    assert str(results["eval_dataset_version"]) == provenance["eval_dataset_version"] == "3"
    assert results["provider"] == provenance["eval_provider"] == "fake"
    assert results["simulated"] is True


def test_a_hand_authored_version_has_no_provenance(client: TestClient, registry: _Registry) -> None:
    registry.versions[1] = {"name": "support-agent", "tags": {}, "commit_message": "by hand"}

    versions = client.get(VERSIONS).json()["data"]

    assert versions[0]["provenance"] == {}


def test_a_job_that_is_not_applied_has_no_apply_summary(
    client: TestClient, db_session: Session
) -> None:
    _seed_job_with_evidence(db_session)

    job = client.get(f"{JOBS}/RFN-A").json()["data"]

    assert job["approval_id"] is None
    assert job["applied_by"] is None
    assert job["applied_version"] is None


def test_the_job_list_carries_the_apply_summary_too(
    client: TestClient, db_session: Session, registry: _Registry
) -> None:
    _seed_job_with_evidence(db_session)
    assert client.post(APPLY_PATH.format(job_id="RFN-A")).status_code == 200

    rows = client.get(JOBS, params={"agent_id": "support-agent"}).json()["data"]

    applied = next(r for r in rows if r["job_id"] == "RFN-A")
    assert applied["applied_version"] == 2
    assert applied["approval_id"]


def test_the_apply_summary_follows_the_newest_approval_after_a_reapply(
    client: TestClient, db_session: Session, registry: _Registry
) -> None:
    """Two applications of one job's lineage name the latest approval and version."""
    _seed_job_with_evidence(db_session)
    first = client.post(APPLY_PATH.format(job_id="RFN-A"))
    assert first.status_code == 200
    job = db_session.get(CaliberRefinementJob, "RFN-A")
    assert job is not None
    job.status = "candidate_ready"
    job.current_stage = "done"
    db_session.commit()

    assert client.post(APPLY_PATH.format(job_id="RFN-A")).status_code == 200

    summary = client.get(f"{JOBS}/RFN-A").json()["data"]
    assert summary["applied_version"] == 3
