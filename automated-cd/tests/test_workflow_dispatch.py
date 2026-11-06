"""Exercise the exact GitHub dispatch guard shell and state-publish dependency."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/automated-cd-run.yml"


@pytest.fixture(scope="module")
def steps():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return {step.get("name"): step for step in workflow["jobs"]["execute"]["steps"]}


@pytest.mark.parametrize(
    ("diagnose", "export", "issue", "retry", "accepted"),
    [
        ("false", "false", "", "", True),
        ("false", "false", "", "821", True),
        ("true", "false", "", "", True),
        ("true", "true", "821", "", True),
        ("false", "false", "821", "", False),
        ("false", "true", "821", "", False),
        ("true", "false", "821", "", False),
        ("true", "true", "", "", False),
        ("true", "true", "821", "821", False),
    ],
)
def test_dispatch_guard_rejects_incident_misfires(steps, diagnose, export, issue, retry, accepted):
    script = steps["Validate incident dispatch inputs"]["run"]
    environment = os.environ | {
        "AUTOCD_DIAGNOSE": diagnose,
        "AUTOCD_EXPORT": export,
        "AUTOCD_INCIDENT_ISSUE": issue,
        "AUTOCD_RETRY_ISSUE": retry,
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script], check=False, env=environment, capture_output=True
    )
    assert (result.returncode == 0) is accepted


def test_failed_restore_cannot_publish_placeholder_state(steps):
    assert steps["Restore coordinator state"]["id"] == "restore"
    for name in ("Encrypt coordinator state", "Save coordinator state"):
        condition = steps[name]["if"]
        assert "always()" in condition
        assert "steps.restore.outcome == 'success'" in condition
        assert "inputs.diagnose_state != true" in condition
        assert "inputs.export_incident != true" in condition
