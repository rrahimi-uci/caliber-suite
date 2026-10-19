#!/usr/bin/env python3
"""Run the lab's 40 production tickets through a real workflow, so Observability has real traces.

In production these traces come from your own traffic. A fresh stack has none, and every step of this
lab works on traces, so this script produces them the way production does: it imports the
``refund-intake`` workflow from cookbook 09 (version 1, the one with the bugs), publishes it, and
submits each ticket in ``dataset/prod-regression.sample.jsonl`` as a workflow run. CALIBER's runtime
records an MLflow trace for every run; nothing here writes a trace itself, and no row is fabricated.
Which runs fail is decided by the workflow, not by this script.

Needs a server that can reach cookbook 09's stand-in account service (port 8780) and was started with
``CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1``. Prints a JSON map ``{"P01": {"run_id", "trace_id",
"status"}, ...}`` on stdout. Every call imports a new workflow and writes 40 new traces, so run it once
per environment.

    export CALIBER_BASE_URL=http://127.0.0.1:5001 CALIBER_TOKEN=calpat_...
    PYTHONPATH=sdk/caliber-sdk python \
      docs-site/cookbooks/16-observability-triage/assets/seed_traces.py > traces.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset" / "prod-regression.sample.jsonl"
#: Cookbook 09's version 1: validate, check_reference, lookup, decide.
WORKFLOW = (
    HERE.parents[1]
    / "09-workflow-debugger-self-healing-lab/assets/workflow/ticket-intake.v1.workflow.yaml"
)
RUN_TIMEOUT_SECONDS = 180.0


def load_rows(dataset: Path = DATASET) -> list[dict[str, Any]]:
    lines = dataset.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def seed(
    caliber: Any,
    *,
    rows: list[dict[str, Any]] | None = None,
    suffix: str | None = None,
    workflow_file: Path = WORKFLOW,
) -> dict[str, dict[str, str]]:
    """Import and publish the workflow, run every row through it, return what each run produced."""
    rows = rows if rows is not None else load_rows()
    suffix = suffix or str(int(time.time()))
    workflow = caliber.workflows.import_workflow(
        manifest_yaml=workflow_file.read_text(encoding="utf-8"),
        name=f"prod-refund-intake-{suffix}",
    )
    version = caliber.workflows.versions.publish(
        caliber.workflows.versions.list(workflow.workflow_id)[0].version_id
    )
    submitted = {
        row["id"]: caliber.workflows.runs.submit(
            workflow_version_id=version.version_id, input=row["input"]["payload"]
        )
        for row in rows
    }
    seeded: dict[str, dict[str, str]] = {}
    for row_id, run in submitted.items():
        done = caliber.workflows.runs.wait(
            run.workflow_run_id, timeout=RUN_TIMEOUT_SECONDS, raise_on_failure=False
        )
        seeded[row_id] = {
            "run_id": done.workflow_run_id,
            "trace_id": done.trace_id or "",
            "status": done.status,
            "workflow_id": workflow.workflow_id,
        }
    return seeded


if __name__ == "__main__":
    from caliber_sdk import CaliberClient

    with CaliberClient(
        os.environ["CALIBER_BASE_URL"], token=os.environ["CALIBER_TOKEN"]
    ) as client:
        json.dump(seed(client), sys.stdout, indent=2)
    sys.stdout.write("\n")
