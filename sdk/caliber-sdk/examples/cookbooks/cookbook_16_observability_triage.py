"""Cookbook 16: turn production failures into evidence you can act on.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/16-observability-triage/README.md)
and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001 CALIBER_TOKEN=calpat_...
    PYTHONPATH=sdk/caliber-sdk python \
      sdk/caliber-sdk/examples/cookbooks/cookbook_16_observability_triage.py

The server needs cookbook 09's stand-in account service (``python3 docs-site/cookbooks/09-workflow-
debugger-self-healing-lab/assets/upstream/account_service.py 8780``) and
``CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1``; nothing here needs a model.

What it does, in order:

1. Seeds production traffic: ``assets/seed_traces.py`` runs the 40 tickets in
   ``assets/dataset/prod-regression.sample.jsonl`` through a real workflow. The traces are the ones
   CALIBER's runtime records; none is written by this script.
2. Reads them back through Observability: the status filter, and the request, response and node
   tree of one failing trace per failure kind. 12 traces are ``ERROR``, 28 ``OK``, and 4 of the
   ``OK`` ones are wrong: nothing errored, the answer is not what the gold outcome says.
3. Shows the failure paths: a trace id that does not exist is a 404, an empty result is an empty
   list, and a trace's run can be found from its trace id.
4. Captures the 16 traces that matter into a test set with their gold outcome.
5. Stands up the ``prod-triage`` review queue, enqueues the same 16 traces, answers two of them, and
   checks that the answers are written back onto the traces as assessments.

``--cleanup`` archives the test set and the queue when the run succeeds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberNotFoundError
from examples.cookbooks._helpers import env_client

ASSETS = Path(
    os.environ.get(
        "COOKBOOK_16_ASSETS",
        Path(__file__).resolve().parents[4] / "docs-site/cookbooks/16-observability-triage/assets",
    )
)
#: The node a failure kind stops at in cookbook 09's version 1 (what the node tree must show).
FAILING_NODE = {"validate": "validate", "poison": "check_reference", "upstream": "decide"}
EXPECTED_STATUS = {"ERROR": 12, "OK": 28}


class VerificationError(AssertionError):
    """A documented outcome did not hold; the message says which."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _seed_module(assets: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "cookbook16_seed_traces", assets / "seed_traces.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decision_of(run: Any) -> dict[str, Any]:
    summary = run.extra.get("summary") or {}
    decision: dict[str, Any] = json.loads(summary.get("output") or "{}")
    return decision


def kind_of(row: dict[str, Any]) -> str | None:
    return next((tag for tag in row["tags"] if tag in FAILING_NODE), None)


def settled_traces(caliber: CaliberClient, since_ms: int, *, wait: float = 60.0) -> dict[str, Any]:
    """The traces since ``since_ms``, once none is still ``IN_PROGRESS``.

    MLflow records a trace asynchronously, so a trace can still read ``IN_PROGRESS`` for a moment
    after its run has ended; a status count taken then would be wrong, not the system.
    """
    deadline = time.monotonic() + wait
    while True:
        found = caliber.observability.traces(limit=200, since_ms=since_ms)
        traces = {t.trace_id: t for t in found}
        if not any(t.status == "IN_PROGRESS" for t in found) or time.monotonic() > deadline:
            return traces
        time.sleep(1.0)


def read_back(
    caliber: CaliberClient,
    rows: list[dict[str, Any]],
    seeded: dict[str, dict[str, str]],
    since_ms: int,
) -> dict[str, Any]:
    """Observability's view of the seeded traffic: status, node tree, request and response."""
    traces = settled_traces(caliber, since_ms)
    mine = {row["id"]: traces.get(seeded[row["id"]]["trace_id"]) for row in rows}
    check(
        all(mine.values()),
        f"traces missing from Observability: {[k for k, v in mine.items() if not v]}",
    )
    status = Counter(trace.status for trace in mine.values() if trace)
    check(dict(status) == EXPECTED_STATUS, f"trace status counts: {dict(status)}")
    for row in rows:
        failed = seeded[row["id"]]["status"] == "failed"
        check(
            (mine[row["id"]].status == "ERROR") == failed,  # type: ignore[union-attr]
            f"{row['id']}: run {seeded[row['id']]['status']} but trace {mine[row['id']].status}",  # type: ignore[union-attr]
        )

    filtered = {
        t.trace_id
        for t in caliber.observability.traces(limit=200, since_ms=since_ms, status="ERROR")
    }
    error_ids = {seeded[r["id"]]["trace_id"] for r in rows if kind_of(r)}
    check(error_ids <= filtered, "the Error filter missed a failing trace")

    detail: dict[str, Any] = {}
    for kind, node in FAILING_NODE.items():
        row = next(r for r in rows if kind_of(r) == kind)
        trace = caliber.observability.trace(seeded[row["id"]]["trace_id"])
        errored = [s["name"] for s in trace["spans"] if s["status"] == "ERROR"]
        check(
            f"node.{node}" in errored, f"{row['id']}: error spans {errored}, expected node.{node}"
        )
        check(trace["status"] == "ERROR", f"{row['id']}: trace status {trace['status']}")
        check(
            row["input"]["payload"][:40] in (trace["request"] or ""),
            f"{row['id']}: the trace does not carry the request",
        )
        detail[kind] = {"row": row["id"], "error_spans": errored}
    return {"status": dict(status), "detail": detail}


def failure_paths(caliber: CaliberClient, seeded: dict[str, dict[str, str]]) -> dict[str, Any]:
    missing = None
    try:
        caliber.observability.trace("tr-00000000000000000000000000000000")
    except CaliberNotFoundError as exc:
        missing = exc.status_code
    check(missing == 404, f"an unknown trace id should be a 404, got {missing}")
    empty = caliber.observability.traces(
        status="ERROR", since_ms=int(time.time() * 1000) + 3_600_000
    )
    check(empty == [], f"a window in the future should be an empty list, got {len(empty)}")
    sample = next(iter(seeded.values()))
    run = caliber.workflows.runs.by_trace(sample["trace_id"])
    check(run.workflow_run_id == sample["run_id"], "by_trace returned another run")
    return {"unknown_trace": missing, "future_window": len(empty), "by_trace": run.workflow_run_id}


def run(
    caliber: CaliberClient,
    *,
    assets: Path = ASSETS,
    suffix: str | None = None,
    seeded: dict[str, dict[str, str]] | None = None,
    cleanup: bool = False,
) -> dict[str, Any]:
    suffix = suffix or str(int(time.time()))
    seed_module = _seed_module(assets)
    rows = seed_module.load_rows(assets / "dataset/prod-regression.sample.jsonl")
    check(len(rows) == 40, f"expected 40 production tickets, found {len(rows)}")
    since_ms = int(time.time() * 1000) - 5_000

    # 1. production traffic, as real runs
    seeded = seeded or seed_module.seed(caliber, rows=rows, suffix=suffix)

    # 2. Observability
    observed = read_back(caliber, rows, seeded, since_ms)

    # which runs finished but gave the wrong answer: nothing errored, a person has to notice
    outcomes = {
        row["id"]: decision_of(caliber.workflows.runs.get(seeded[row["id"]]["run_id"]))
        for row in rows
        if seeded[row["id"]]["status"] == "completed"
    }
    wrong = [
        row["id"]
        for row in rows
        if row["id"] in outcomes
        and (outcomes[row["id"]].get("decision"), outcomes[row["id"]].get("reason_code"))
        != (row["expected"]["decision"], row["expected"]["reason_code"])
    ]
    check(
        wrong == [r["id"] for r in rows if "silent_wrong" in r["tags"]],
        f"OK traces with the wrong outcome: {wrong}",
    )

    # 3. failure paths
    paths = failure_paths(caliber, seeded)

    # 4. the test set
    to_capture = [r for r in rows if kind_of(r) or r["id"] in wrong]
    check(len(to_capture) == 16, f"expected 16 traces to capture, found {len(to_capture)}")
    dataset = caliber.eval_datasets.create(
        f"prod-regression-{suffix}", owner=caliber.me.get().user_id, description="Cookbook 16"
    )
    for row in to_capture:
        caliber.eval_datasets.add_from_trace(
            dataset.dataset_id,
            seeded[row["id"]]["trace_id"],
            expected=row["expected"],
            tags=row["tags"],
        )
    examples = caliber.eval_datasets.examples(dataset.dataset_id)
    check(len(examples) == 16, f"the test set holds {len(examples)} examples, expected 16")
    first = examples[0]
    check("T-50" in json.dumps(first.input), "a captured example does not carry its request")

    # 5. triage
    questions = json.loads(
        (assets / "review-queues/triage-review.review-questions.json").read_text()
    )
    queue = caliber.review_queues.create(f"prod-triage-{suffix}", questions=questions["questions"])
    items = caliber.review_queues.enqueue(
        queue.queue_id, trace_ids=[seeded[r["id"]]["trace_id"] for r in to_capture]
    )
    check(len(items) == 16, f"the queue took {len(items)} traces, expected 16")
    item_for_trace = {item["trace_id"]: item["item_id"] for item in items}
    reviewed = {"P38": ("tool", "high"), "P25": ("data", "low")}
    for row_id, (mode, severity) in reviewed.items():
        row = next(r for r in rows if r["id"] == row_id)
        caliber.review_queues.submit(
            queue.queue_id,
            item_for_trace[seeded[row_id]["trace_id"]],
            root_cause_known=True,
            failure_mode=mode,
            severity=severity,
            expected_output=json.dumps(row["expected"]),
            reviewer_notes=f"cookbook 16 review of {row_id}",
        )
    written: dict[str, list[str]] = {}
    for row_id in reviewed:
        trace = caliber.observability.trace(seeded[row_id]["trace_id"])
        names = sorted({a["name"] for a in trace["assessments"]})
        check("failure_mode" in names and "severity" in names, f"{row_id}: assessments {names}")
        written[row_id] = names

    result = {
        "seeded": len(seeded),
        "traces": observed["status"],
        "node_tree": {kind: v["error_spans"][-1] for kind, v in observed["detail"].items()},
        "ok_but_wrong": wrong,
        "failure_paths": paths,
        "test_set": {"examples": len(examples), "dataset_id": dataset.dataset_id},
        "triage": {"queued": len(items), "answered": len(reviewed), "assessments": written},
    }
    if cleanup:
        caliber.eval_datasets.update(dataset.dataset_id, status="archived")
        caliber.review_queues.update(queue.queue_id, status="archived")
    return result


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber, cleanup="--cleanup" in sys.argv), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
