#!/usr/bin/env python3
"""Record the 30 flagged agent replies as MLflow traces, so a review queue can hold them.

In a real deployment these traces are produced by your agent and flagged by thumbs-down, a keyword
rule or a low judge score. A fresh checkout has none, and Operate > Review Queues works on traces,
so this script writes one trace per row of ``dataset/agent-replies.jsonl``: the request is the
customer's message, the response is the agent's reply, and the tags say which row it is
(``cookbook13.row``, R01..R30) and why it was flagged (``cookbook13.flag``).

Needs the ``mlflow`` package and ``MLFLOW_TRACKING_URI`` (the MLflow server CALIBER uses).
Prints a JSON map ``{"R01": "<trace_id>", ...}`` on stdout. Every run writes 30 new traces, so
run it once per environment.

    MLFLOW_TRACKING_URI=http://127.0.0.1:5000 \
      python docs-site/cookbooks/13-aria-review-governance-queue/assets/seed_traces.py > traces.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "dataset" / "agent-replies.jsonl"
EXPERIMENT = "cookbook-13-flagged-replies"


def seed(dataset: Path = DATASET) -> dict[str, str]:
    """Write one trace per row and return ``{row id: trace id}``."""
    import mlflow

    mlflow.set_experiment(EXPERIMENT)
    trace_ids: dict[str, str] = {}
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        with mlflow.start_span(name="agent-reply", span_type="CHAIN") as span:
            span.set_inputs({"user_message": row["user_message"]})
            span.set_outputs(row["reply"])
            trace_id = span.trace_id
        mlflow.set_trace_tag(trace_id, "cookbook13.row", row["id"])
        mlflow.set_trace_tag(trace_id, "cookbook13.flag", row["flag"])
        trace_ids[row["id"]] = trace_id
    mlflow.flush_trace_async_logging()
    return trace_ids


if __name__ == "__main__":
    json.dump(seed(), sys.stdout, indent=2)
    sys.stdout.write("\n")
