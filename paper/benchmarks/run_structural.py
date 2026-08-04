#!/usr/bin/env python3
"""Execute the deterministic claim checks cited by the CALIBER paper.

This is intentionally not a performance benchmark. It exercises controlled
interleavings and fault boundaries that can establish structural properties
without pretending to answer the unrun latency, throughput, or human-agreement
questions in Table 5.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PAPER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PAPER_ROOT.parent
CALIBER_ROOT = REPO_ROOT / "caliber"
DEFAULT_OUTPUT = PAPER_ROOT / "benchmarks" / "results" / "structural.json"

CHECKS = {
    "queue_controlled_double_ownership": (
        "tests/test_calibration_jobs.py::test_two_drains_racing_on_the_same_row_cannot_both_win"
    ),
    "calibration_operator_fences_late_result": (
        "tests/test_calibration_jobs.py::test_operator_resolution_fences_a_late_worker_result"
    ),
    "release_intent_precedes_effect": (
        "tests/test_release_operations.py::"
        "test_prompt_release_intent_is_committed_before_provider_effect"
    ),
    "release_provider_timeout_is_observable": (
        "tests/test_release_operations.py::test_provider_error_leaves_reconciliation_obligation"
    ),
    "release_reconciliation_converges": (
        "tests/test_release_operations.py::"
        "test_reconciler_settles_observed_target_and_flags_unknown_state"
    ),
    "prepared_release_abandon_is_pre_effect": (
        "tests/test_release_operations.py::"
        "test_prepared_release_can_be_abandoned_without_provider_ambiguity"
    ),
    "resolver_last_known_outage_fallback": (
        "tests/test_resolver.py::test_resolver_serves_last_known_value_during_outage"
    ),
    "aria_publish_is_human_gated": (
        "tests/test_assistant_agent_tools.py::TestGating::"
        "test_gated_draft_tools_cannot_be_dispatched"
    ),
}


def git_revision() -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to record the evidence revision")
    result = subprocess.run(  # noqa: S603 - executable resolved from PATH; arguments fixed
        [git, "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def working_tree_dirty() -> bool:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to record the evidence worktree state")
    result = subprocess.run(  # noqa: S603 - executable resolved from PATH; arguments fixed
        [git, "status", "--porcelain"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def run_checks() -> tuple[subprocess.CompletedProcess[str], list[str]]:
    node_ids = list(CHECKS.values())
    command = ["python", "-m", "pytest", *node_ids, "--no-cov", "-q"]
    execution_command = [sys.executable, *command[1:]]
    return (
        subprocess.run(  # noqa: S603 - command is a fixed interpreter and fixed node-id list
            execution_command,
            cwd=CALIBER_ROOT,
            check=False,
            capture_output=True,
            text=True,
        ),
        command,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="run without writing a manifest")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    completed, command = run_checks()
    manifest = {
        "schema_version": 1,
        "evidence_class": "deterministic_structural_checks",
        "not_evidence_for": [
            "production latency",
            "production throughput",
            "replica-scale stress behavior",
            "human agreement",
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_revision": git_revision(),
        "working_tree_dirty": working_tree_dirty(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "command": command,
        "checks": [
            {
                "id": check_id,
                "node_id": node_id,
                "status": "passed" if completed.returncode == 0 else "failed",
            }
            for check_id, node_id in CHECKS.items()
        ],
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
    if not args.verify:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        sys.stdout.write(f"{args.output}\n")
    sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
