"""The sync SDK drives the skill calibration journey against a real server (alpha gap P1-20).

The alpha claims skills are supported "equivalently through the SDK". The SDK's skill
methods were only ever tested against a mock transport, so a renamed payload key would have
passed everything. This runs author, render, calibrate, apply and roll back in-process: the
real application with its background worker and the real ``caliber_sdk`` over the ASGI
transport, asserting the skill's content and version after every step.

A skill is a database row, not a registry entry, so unlike the prompt journey there is no
MLflow promoter to check; Apply rewrites the row and records a version.

It runs in a subprocess for the reason ``test_sdk_prompt_journey.py`` does: MLflow's SQLite
bootstrap leaves migration mappers behind that confuse other tests in a long-lived pytest
process.
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
            "CALIBER_BACKGROUND_TASKS_ENABLED": "true",
        }
    )
    app = create_app(config=config)
    Base.metadata.create_all(app.state.engine)

    original = "When asked about refunds, reply politely about {{topic}}."
    out = {}
    with TestClient(app, headers={"X-CALIBER-User": "@admin"}) as http:
        caliber = caliber_sdk.CaliberClient("http://testserver", http_client=http)

        # 1. Author.
        skill = caliber.skills.create(
            "sdk-journey-skill",
            content=original,
            owner="@admin",
            summary="Use when a customer asks about a refund.",
            description="Handles refund questions.",
        )
        skill_id = skill.skill_id
        out["created_version"] = skill.version

        # 2. Render: variables are substituted and nothing is left unresolved.
        rendered = caliber.skills.render(skill_id, variables={"topic": "refunds"})
        out["rendered"] = rendered.rendered_content
        out["unresolved"] = list(rendered.unresolved_variables)

        # 3. Calibrate. The worker stops the job at candidate_ready.
        run = caliber.skills.calibrate(skill_id)
        job_id = run["job"]["job_id"]
        job = caliber.jobs.wait(job_id, timeout=120, interval=0.5)
        out["job_status"] = job.status
        out["candidate_has_refinement"] = "Refinement added by SkillMetaPrompt" in (
            (job.candidate or {}).get("content") or ""
        )
        out["eval_simulated"] = (job.eval_results or {}).get("simulated")
        out["eval_provider"] = (job.eval_results or {}).get("provider")
        out["content_before_apply"] = caliber.skills.get(skill_id).content

        # 4. Apply: the explicit human decision. The skill becomes the candidate.
        applied = caliber.jobs.apply(job_id)
        out["apply_status"] = applied["status"]
        after = caliber.skills.get(skill_id)
        out["version_after_apply"] = after.version
        out["content_after_apply"] = after.content
        settled = caliber.jobs.get(job_id)
        out["applied_by"] = settled.applied_by
        out["approval_id"] = settled.approval_id

        # 5. Roll back: the original content returns as a new version.
        caliber.skills.rollback(skill_id)
        rolled = caliber.skills.get(skill_id)
        out["version_after_rollback"] = rolled.version
        out["content_after_rollback"] = rolled.content
        out["versions"] = sorted(v.version_number for v in caliber.skills.versions(skill_id))

    print("RESULT=" + json.dumps(out, default=str))
    """
)

_ORIGINAL = "When asked about refunds, reply politely about {{topic}}."


def _run(tmp_path: Path) -> dict[str, object]:
    script = tmp_path / "sdk_skill_journey.py"
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


def test_the_sdk_drives_the_skill_calibration_journey(tmp_path: Path) -> None:
    result = _run(tmp_path)

    # Author and render.
    assert result["created_version"] == 1, result
    assert result["rendered"] == "When asked about refunds, reply politely about refunds.", result
    assert result["unresolved"] == [], result
    # Calibrate: a reviewable candidate, with evidence that says it is not measured.
    assert result["job_status"] == "candidate_ready", result
    assert result["candidate_has_refinement"] is True, result
    assert result["eval_simulated"] is True, result
    assert result["eval_provider"] == "fake", result
    assert result["content_before_apply"] == _ORIGINAL, result  # nothing changed yet
    # Apply: the skill is now the candidate, as version 2, and the job names who did it.
    assert result["apply_status"] == "applied", result
    assert result["version_after_apply"] == 2, result
    assert "Refinement added by SkillMetaPrompt" in str(result["content_after_apply"]), result
    assert result["applied_by"] == "@admin", result
    assert str(result["approval_id"]).startswith("AP-"), result
    # Roll back: the original text is current again. Skills are forward-only, so it is v3.
    assert result["version_after_rollback"] == 3, result
    assert result["content_after_rollback"] == _ORIGINAL, result
    assert result["versions"] == [1, 2, 3], result
