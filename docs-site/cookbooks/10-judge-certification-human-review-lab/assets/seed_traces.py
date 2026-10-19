#!/usr/bin/env python3
"""Record the 50 dataset outputs as MLflow traces, so they can be reviewed in CALIBER.

In a real deployment these traces are produced by your application (SCN-07 support replies and
SCN-08 incident summaries). A fresh checkout has none, and Operate > Review Queues works on traces,
so this script writes one trace per dataset row: the request is the row's ``input`` and the response
is its recorded output text. Each trace carries the tag ``cookbook10.row`` (J01..J50).

Needs the ``mlflow`` package and ``MLFLOW_TRACKING_URI`` (the MLflow server CALIBER uses).
Prints a JSON map ``{"J01": "<trace_id>", ...}`` on stdout. Every run writes 50 new traces, so run it
once per environment.

    MLFLOW_TRACKING_URI=http://127.0.0.1:5000 \
      python docs-site/cookbooks/10-judge-certification-human-review-lab/assets/seed_traces.py > traces.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "dataset" / "aggregated-outputs.jsonl"
EXPERIMENT = "cookbook-10-judge-certification"


def output_text(row: dict) -> str:
    """The text of a recorded output: the support reply or the incident summary."""
    return row["outputs"].get("reply") or row["outputs"].get("summary") or ""


def seed(dataset: Path = DATASET) -> dict[str, str]:
    """Write one trace per dataset row and return ``{row id: trace id}``."""
    import mlflow

    mlflow.set_experiment(EXPERIMENT)
    trace_ids: dict[str, str] = {}
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        with mlflow.start_span(name="recorded-output", span_type="CHAIN") as span:
            span.set_inputs(row["input"])
            span.set_outputs(output_text(row))
            trace_id = span.trace_id
        mlflow.set_trace_tag(trace_id, "cookbook10.row", row["id"])
        trace_ids[row["id"]] = trace_id
    mlflow.flush_trace_async_logging()
    return trace_ids


if __name__ == "__main__":
    json.dump(seed(), sys.stdout, indent=2)
    sys.stdout.write("\n")
