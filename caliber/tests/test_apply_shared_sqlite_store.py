"""Apply on one SQLite file shared by CALIBER and MLflow (alpha gap P0-2).

``scripts/run-dev.sh`` and the browser-test harness point MLflow's backend store at
the same SQLite file as ``CALIBER_DATABASE_URL``. Apply used to claim the job and
add the approval row in an uncommitted write transaction, then call the promoter,
and the promoter registers the new prompt version through MLflow's *own* engine.
That engine waited on CALIBER's still-open write lock until its busy timeout ran
out (about 21 seconds), so Apply failed every time on that file. The failure was
reproduced with every background loop disabled, which ruled out worker contention
(the explanation the earlier reports gave).

This test reproduces the shape for real, with the real MLflow prompt registry and
the real ``MLflowPromoter``, and asserts Apply now succeeds quickly. It runs in a
subprocess because MLflow's SQLite bootstrap leaves migration mappers behind that
can confuse other tests in a long-lived pytest process (the reason the other
real-MLflow tests are opt-in).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("mlflow", reason="mlflow not installed")

pytestmark = pytest.mark.slow

_SCRIPT = textwrap.dedent(
    r"""
    import json, os, sys, time

    db = sys.argv[1]
    os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{db}"
    os.environ["MLFLOW_REGISTRY_URI"] = f"sqlite:///{db}"

    import mlflow
    from starlette.testclient import TestClient

    from caliber.candidate_digest import candidate_digest
    from caliber.config import CaliberConfig
    from caliber.db.base import Base
    from caliber.db.models import (
        CaliberAgentConfig,
        CaliberRefinementJob,
        CaliberVerificationItem,
    )
    from caliber.server import create_app

    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db}",
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_ADMIN_USERS": "@admin",
            # No promoter or artifact-store variable on purpose: this process runs outside
            # the test config hook, so it exercises the shipped defaults (the real MLflow
            # providers) exactly as a first install does (alpha gap P0-3).
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        }
    )
    app = create_app(config=config)
    Base.metadata.create_all(app.state.engine)

    # MLflow builds its own tables in the same file on first use.
    mlflow.genai.register_prompt(name="shared-store-prompt", template="v1 {{x}}")
    mlflow.genai.set_prompt_alias("shared-store-prompt", alias="prod", version=1)

    candidate = {
        "artifact_type": "prompt",
        "content": "v2 {{x}}",
        "rationale": "shared store regression",
        "diff_summary": "+1 / -1",
    }
    with app.state.session_factory() as session:
        session.add(
            CaliberAgentConfig(
                agent_id="shared-store-prompt",
                experiment_id="exp-shared-store",
                name="shared-store-prompt",
                owner="@admin",
                visibility="public",
                artifact_types=["prompt"],
                eval_thresholds={},
                optimizer_config={},
                approval_policy={},
            )
        )
        session.flush()
        session.add(
            CaliberVerificationItem(
                item_id="FB-SHARED",
                agent_id="shared-store-prompt",
                category="x",
                free_text="x",
                severity="low",
                status="verified",
            )
        )
        session.flush()
        session.add(
            CaliberRefinementJob(
                job_id="RFN-SHARED",
                agent_id="shared-store-prompt",
                primary_item_id="FB-SHARED",
                artifact_type="prompt",
                status="candidate_ready",
                current_stage="done",
                attempt_count=1,
                bundle_targets=[],
                eval_results={"gate": {"passed": True}},
                candidate=candidate,
                candidate_digest=candidate_digest(candidate),
            )
        )
        session.commit()

    headers = {"X-CALIBER-User": "@admin"}
    out = {}
    out["promoter_is_simulated"] = bool(getattr(app.state.promoter, "is_simulated", False))
    with TestClient(app, headers=headers) as client:
        started = time.time()
        resp = client.post("/ajax-api/2.0/mlflow/caliber/jobs/RFN-SHARED/apply", json={})
        out["apply_seconds"] = round(time.time() - started, 1)
        out["apply_status"] = resp.status_code
        out["apply_body"] = resp.json()
        again = client.post("/ajax-api/2.0/mlflow/caliber/jobs/RFN-SHARED/apply", json={})
        out["second_apply_status"] = again.status_code
        with app.state.session_factory() as session:
            job = session.get(CaliberRefinementJob, "RFN-SHARED")
            out["job_status"] = job.status
        live = mlflow.genai.load_prompt("prompts:/shared-store-prompt@prod")
        out["live_version"] = int(live.version)
        out["live_template"] = live.template
    print("RESULT=" + json.dumps(out))
    """
)


def _run(tmp_path: Path) -> dict[str, object]:
    script = tmp_path / "shared_store_apply.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "shared.db")],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT=")]
    assert lines, (
        f"no result (exit {proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-3000:]}"
    )
    return json.loads(lines[-1][len("RESULT=") :])


def test_apply_succeeds_when_caliber_and_mlflow_share_one_sqlite_file(tmp_path: Path) -> None:
    result = _run(tmp_path)

    # With no provider variables the release path is the real MLflow one.
    assert result["promoter_is_simulated"] is False, result
    assert result["apply_status"] == 200, result
    assert result["apply_body"]["data"]["simulated"] is False, result  # type: ignore[index]
    # The old behavior spent ~21 s in MLflow's busy timeout and then failed with
    # "database is locked"; a healthy Apply is a few seconds at most.
    assert float(result["apply_seconds"]) < 15, result  # type: ignore[arg-type]
    assert result["job_status"] == "applied", result
    # The registry really moved: a new version is live on the prod alias.
    assert result["live_version"] == 2, result
    assert result["live_template"] == "v2 {{x}}", result
    # A second Apply of the same job is refused, not run again.
    assert result["second_apply_status"] == 409, result
