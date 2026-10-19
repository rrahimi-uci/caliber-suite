#!/usr/bin/env python3
"""Record the 12 weak support runs as MLflow traces, so a review queue can hold them.

In a real deployment these traces are produced by your workflow and flagged by a person, a metric or
a guardrail. A fresh checkout has none, and Operate > Review Queues works on traces, so this script
writes one trace per *weak* row of ``dataset/support-runs.jsonl``: the request is the customer's
question, the response is the reply the agent gave, and the tags say which run it is
(``cookbook15.row``, S19..S30) and what went wrong (``cookbook15.failure_mode`` and
``cookbook15.reason``). The runs the agent got right are not written: nobody flags them.

Needs the ``mlflow`` package and ``MLFLOW_TRACKING_URI`` (the MLflow server CALIBER uses).
Prints a JSON map ``{"S19": "<trace_id>", ...}`` on stdout. Every run writes 12 new traces, so run
it once per environment.

    MLFLOW_TRACKING_URI=http://127.0.0.1:5000 \
      python docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets/seed_traces.py > traces.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "dataset" / "support-runs.jsonl"
EXPERIMENT = "cookbook-15-weak-support-runs"


def seed(dataset: Path = DATASET) -> dict[str, str]:
    """Write one trace per weak row and return ``{row id: trace id}``."""
    import mlflow

    mlflow.set_experiment(EXPERIMENT)
    trace_ids: dict[str, str] = {}
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not row["weak"]:
            continue
        with mlflow.start_span(name="support-policy-agent", span_type="CHAIN") as span:
            span.set_inputs({"question": row["question"]})
            span.set_outputs(row["reply"])
            trace_id = span.trace_id
        mlflow.set_trace_tag(trace_id, "cookbook15.row", row["id"])
        mlflow.set_trace_tag(trace_id, "cookbook15.failure_mode", row["reason"].split(":", 1)[0])
        mlflow.set_trace_tag(trace_id, "cookbook15.reason", row["reason"])
        trace_ids[row["id"]] = trace_id
    mlflow.flush_trace_async_logging()
    return trace_ids


if __name__ == "__main__":
    json.dump(seed(), sys.stdout, indent=2)
    sys.stdout.write("\n")
