#!/usr/bin/env python3
"""Record which CI gates actually executed, and refuse a green run that skipped one.

A gate that never runs is indistinguishable from a gate that passes. That is not a
hypothetical: the check asserting the wheel contains a bundled SPA is correct, has
existed all along, and was skipped on twelve consecutive runs because it sat behind
six unrelated jobs. It executed for the first time only when a branch made every
prerequisite pass -- and immediately failed. The release checklist had been counting
it as coverage the whole time.

Nothing in the run summary surfaced that. GitHub renders a skipped job in the same
neutral grey as one that was never going to run, and the run's overall status is
green either way. This script is the missing readout: it lists every required gate
with the conclusion it actually reached, writes that to the job summary so it is
visible without opening the API, and fails when a gate was skipped on a run that
would otherwise have reported success.

It deliberately does *not* fail on a skip that follows a failure. That cascade is
how ``needs:`` is supposed to work, the run is already red, and re-reporting it
would train readers to ignore this job -- which would recreate the problem one
level up.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

#: Gates whose execution is load-bearing for a release decision. Listed by job
#: name rather than discovered, because "which checks must have run" is a claim
#: about what the project considers evidence, and a discovered list would silently
#: shrink when a job is renamed or removed -- the exact failure this guards.
#: Matrix legs are listed individually rather than matched by prefix, because a
#: matrix that quietly loses a leg is the same defect in a different shape: the
#: job still reports success, for fewer versions than the package claims to
#: support. Narrowing the matrix should require editing this list.
REQUIRED_GATES = (
    "Lint & format",
    "Type check",
    "Backend smoke",
    "Backend core journey",
    "Migration parity (PostgreSQL)",
    "Docs validation",
    "UI (test + build)",
    "Compose merged-config validation",
    "Wheel build (with bundled SPA)",
    "Alpha install (wheels, no checkout)",
    "Alpha journey (browser)",
    "Security scan",
)

#: Conclusions that mean the gate produced no evidence about this commit.
DID_NOT_RUN = {"skipped", None}

API = "https://api.github.com"


#: Transient answers from the GitHub API. A 5xx during an incident turned a green run's ledger
#: red ("could not read this run's jobs: HTTP Error 502") and took a manual re-run to clear,
#: so reads are retried a few times before failing closed.
_RETRYABLE_STATUS = {500, 502, 503, 504}
_ATTEMPTS = 4


def _read_json(request: urllib.request.Request) -> dict:
    last: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_STATUS:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(min(2**attempt, 8))
    assert last is not None
    raise last


def fetch_jobs(repo: str, run_id: str, token: str) -> list[dict]:
    jobs: list[dict] = []
    page = 1
    while True:
        url = f"{API}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        payload = _read_json(request)
        batch = payload.get("jobs", [])
        jobs.extend(batch)
        if len(jobs) >= payload.get("total_count", len(jobs)) or not batch:
            return jobs
        page += 1


#: The jobs API lags behind the jobs themselves. On 2026-10-24 the ledger started two
#: seconds after "Backend core journey" completed and read its conclusion as ``null``; on
#: the re-run, two gates copied from the first attempt read ``null`` as well, although the
#: API reported ``success`` for both minutes later. Every gate is in this job's ``needs:``,
#: so a ``null`` here means "not yet visible", not "did not run" (a gate that did not run
#: reads ``skipped``). Re-read a few times before treating it as missing.
_SETTLE_DELAYS = (5, 10, 15, 30, 30)


def fetch_settled_jobs(repo: str, run_id: str, token: str, self_name: str) -> list[dict]:
    """Read the jobs until no required gate's conclusion is still ``null``, or give up."""
    jobs = fetch_jobs(repo, run_id, token)
    for delay in _SETTLE_DELAYS:
        pending = {
            job["name"]
            for job in jobs
            if job.get("name") in REQUIRED_GATES
            and job.get("name") != self_name
            and job.get("conclusion") is None
        }
        if not pending:
            break
        print(f"waiting {delay}s for the API to report: {', '.join(sorted(pending))}")
        time.sleep(delay)
        jobs = fetch_jobs(repo, run_id, token)
    return jobs


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    token = os.environ.get("GH_TOKEN", "")
    if not (repo and run_id and token):
        print("missing GITHUB_REPOSITORY, GITHUB_RUN_ID or GH_TOKEN", file=sys.stderr)
        return 1

    # This job is still in progress while it reads the list, so exclude it rather
    # than reporting itself as a gate that has not concluded.
    self_name = os.environ.get("GITHUB_JOB_NAME", "Gate execution ledger")
    try:
        jobs = fetch_settled_jobs(repo, run_id, token, self_name)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # An unreachable API must not be reported as "every gate ran". Failing
        # closed here costs a re-run; failing open reintroduces the silence.
        print(f"could not read this run's jobs: {exc}", file=sys.stderr)
        return 1

    conclusions = {
        job["name"]: job.get("conclusion")
        for job in jobs
        if job.get("name") != self_name
    }

    rows, missing = [], []
    for gate in REQUIRED_GATES:
        conclusion = conclusions.get(gate, "not present in this run")
        rows.append((gate, conclusion))
        if conclusion in DID_NOT_RUN or conclusion == "not present in this run":
            missing.append(gate)

    anything_failed = any(
        job.get("conclusion") == "failure"
        for job in jobs
        if job.get("name") != self_name
    )

    width = max(len(name) for name, _ in rows)
    lines = ["", "Gate execution ledger", "=" * (width + 24), ""]
    for name, conclusion in rows:
        mark = "." if conclusion == "success" else "!"
        lines.append(f" {mark} {name:<{width}}  {conclusion}")
    lines.append("")
    print("\n".join(lines))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        table = ["### Gate execution ledger", "", "| Gate | Conclusion |", "| --- | --- |"]
        table += [f"| {name} | `{conclusion}` |" for name, conclusion in rows]
        if missing:
            table += ["", f"**{len(missing)} required gate(s) produced no evidence.**"]
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(table) + "\n")

    if not missing:
        print(f"all {len(REQUIRED_GATES)} required gates executed")
        return 0

    if anything_failed:
        print(
            f"{len(missing)} gate(s) did not run, but another job failed first: "
            f"{', '.join(missing)}.\nThis is the normal `needs:` cascade and the run "
            f"is already red, so it is reported rather than failed.",
        )
        return 0

    print(
        "\nERROR: this run would have reported success while "
        f"{len(missing)} required gate(s) produced no evidence about this commit:\n  "
        + "\n  ".join(missing)
        + "\n\nA gate that never runs is indistinguishable from a gate that passes. "
        "Either fix what is preventing it from running, or remove it from "
        "REQUIRED_GATES in .github/scripts/gate_ledger.py so the claim matches "
        "what is actually checked.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
