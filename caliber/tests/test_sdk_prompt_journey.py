"""The sync SDK drives the prompt calibration journey against a real server (alpha gap P1-19).

The alpha claims "equivalently through the SDK". Every SDK test of ``prompts.create``,
``create_calibration_run``, ``jobs.wait``, ``jobs.apply`` and ``prompts.rollback`` used an
``httpx.MockTransport``, and ``test_sdk_api_coverage`` only proves each route exists, so a
renamed payload key would have passed everything. This runs the whole loop in-process:
the real application with its background worker, the real MLflow prompt registry on a
temporary SQLite store, the real ``MLflowPromoter``, and the real ``caliber_sdk`` over the
ASGI transport. It asserts the registry's live version after every step.

It runs in a subprocess for the reason ``test_apply_shared_sqlite_store.py`` does: MLflow's
SQLite bootstrap leaves migration mappers behind that confuse other tests in a long-lived
pytest process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("mlflow", reason="mlflow not installed")
pytest.importorskip("caliber_sdk", reason="caliber-sdk is not installed")

pytestmark = pytest.mark.slow

_SCRIPT = textwrap.dedent(
    r"""
    import json, os, sys, time

    db = sys.argv[1]
    os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{db}"
    os.environ["MLFLOW_REGISTRY_URI"] = f"sqlite:///{db}"

    import mlflow
    from starlette.testclient import TestClient

    import caliber_sdk
    from caliber.config import CaliberConfig
    from caliber.db.base import Base
    from caliber.server import create_app

    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db}",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_ADMIN_USERS": "@admin",
            # Shipped defaults for providers: the real MLflow promoter, fake LLM and
            # evaluator (simulated, and marked so). The worker runs, as in production.
            "CALIBER_BACKGROUND_TASKS_ENABLED": "true",
        }
    )
    app = create_app(config=config)
    Base.metadata.create_all(app.state.engine)

    def live_version(name):
        try:
            return int(mlflow.genai.load_prompt(f"prompts:/{name}@prod").version)
        except Exception:
            return None

    out = {}
    name = "sdk-journey-prompt"
    with TestClient(app, headers={"X-CALIBER-User": "@admin"}) as http:
        caliber = caliber_sdk.CaliberClient("http://testserver", http_client=http)

        # 1. Author a prompt and make v1 live.
        created = caliber.prompts.create(name, "Classify the ticket as billing, bug or other: {{ticket}}")
        out["created_version"] = created["version"]
        caliber.prompts.promote(name, created["version"])
        out["live_after_promote"] = live_version(name)

        # 2. A test set with three examples.
        dataset = caliber.eval_datasets.create("sdk-journey-set", owner="@admin")
        for ticket in ("charged twice", "export crashes", "change my avatar"):
            caliber.eval_datasets.add_example(
                dataset.dataset_id, input={"ticket": ticket}, expected={"label": "billing"}
            )

        # 3. Calibrate. The worker picks the job up and stops it at candidate_ready.
        run = caliber.prompts.create_calibration_run(
            agent_id=name,
            eval_dataset_id=dataset.dataset_id,
            optimizer_type="MetaPrompt",
            scorers=[{"name": "Correctness"}],
            prompt_alias="prod",
        )
        job_id = run["job"]["job_id"]
        job = caliber.jobs.wait(job_id, timeout=120, interval=0.5)
        out["job_status"] = job.status
        out["eval_n_examples"] = (job.eval_results or {}).get("n_examples")
        out["eval_simulated"] = (job.eval_results or {}).get("simulated")
        out["eval_dataset_version"] = (job.eval_results or {}).get("eval_dataset_version")
        out["live_before_apply"] = live_version(name)

        # 4. Apply: the explicit human decision. v2 goes live.
        started = time.time()
        applied = caliber.jobs.apply(job_id)
        out["apply_seconds"] = round(time.time() - started, 1)
        out["apply_status"] = applied["status"]
        out["apply_simulated"] = applied["simulated"]
        out["live_after_apply"] = live_version(name)
        after = caliber.jobs.get(job_id)
        out["applied_version"] = after.applied_version
        out["applied_by"] = after.applied_by
        out["approval_id"] = after.approval_id

        # 5. Roll back: v1 is live again.
        caliber.prompts.rollback(name)
        out["live_after_rollback"] = live_version(name)

    print("RESULT=" + json.dumps(out, default=str))
    """
)


def _run(tmp_path: Path) -> dict[str, object]:
    script = tmp_path / "sdk_journey.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "journey.db")],
        capture_output=True,
        text=True,
        timeout=420,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT=")]
    assert lines, f"no result (exit {proc.returncode})\nstderr:\n{proc.stderr[-4000:]}"
    return json.loads(lines[-1][len("RESULT=") :])


def test_the_sdk_drives_the_prompt_calibration_journey(tmp_path: Path) -> None:
    result = _run(tmp_path)

    # Author and promote.
    assert result["created_version"] == 1, result
    assert result["live_after_promote"] == 1, result
    # Calibrate: the worker reached a reviewable candidate with honest evidence.
    assert result["job_status"] == "candidate_ready", result
    assert result["eval_n_examples"] == 3, result
    assert result["eval_simulated"] is True, result
    assert result["eval_dataset_version"] is not None, result
    assert result["live_before_apply"] == 1, result
    # Apply: a new version is live in the real registry, quickly, and says it is real.
    assert result["apply_status"] == "applied", result
    assert result["apply_simulated"] is False, result
    assert float(result["apply_seconds"]) < 30, result  # type: ignore[arg-type]
    assert result["live_after_apply"] == 2, result
    # The typed Job answers "who put this live": the approval, the applier, the version.
    assert result["applied_version"] == 2, result
    assert result["applied_by"] == "@admin", result
    assert str(result["approval_id"]).startswith("AP-"), result
    # Roll back: the first version is live again.
    assert result["live_after_rollback"] == 1, result
