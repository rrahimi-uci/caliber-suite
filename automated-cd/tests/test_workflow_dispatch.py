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
        "AUTOCD_TRIAL_TASKS": "0",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script], check=False, env=environment, capture_output=True
    )
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize(
    ("trial", "event", "actor", "triggering_actor", "diagnose", "accepted"),
    [
        ("5", "workflow_dispatch", "rrahimi-uci", "rrahimi-uci", "false", True),
        ("5", "schedule", "rrahimi-uci", "rrahimi-uci", "false", False),
        ("5", "workflow_dispatch", "another-user", "another-user", "false", False),
        ("5", "workflow_dispatch", "rrahimi-uci", "another-user", "false", False),
        ("5", "workflow_dispatch", "rrahimi-uci", "rrahimi-uci", "true", False),
        ("6", "workflow_dispatch", "rrahimi-uci", "rrahimi-uci", "false", False),
        ("word", "workflow_dispatch", "rrahimi-uci", "rrahimi-uci", "false", False),
    ],
)
def test_dispatch_guard_bounds_manual_trial(
    steps, trial, event, actor, triggering_actor, diagnose, accepted
):
    script = steps["Validate incident dispatch inputs"]["run"]
    environment = os.environ | {
        "AUTOCD_TRIAL_TASKS": trial,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_ACTOR": actor,
        "GITHUB_TRIGGERING_ACTOR": triggering_actor,
        "AUTOCD_DIAGNOSE": diagnose,
        "AUTOCD_EXPORT": "false",
        "AUTOCD_INCIDENT_ISSUE": "",
        "AUTOCD_RETRY_ISSUE": "",
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
