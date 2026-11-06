"""The owner-gated planning, development, review, QA and PR state machine."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import ClaudeAgent
from .config import Settings
from .github import GitHub, audit_commands, command, pr_ready
from .planner import Assignment, assign, eligible
from .state import Store, exclusive


class Engine:
    def __init__(
        self, settings: Settings, *, github: GitHub | None = None, agent_factory=ClaudeAgent
    ):
        self.settings = settings
        self.github = github or GitHub(settings)
        self.agent_factory = agent_factory
        self.mutation_lock = threading.Lock()

    def due_date(self, now: datetime | None = None) -> str | None:
        local = (now or datetime.now(ZoneInfo(self.settings.timezone))).astimezone(
            ZoneInfo(self.settings.timezone)
        )
        return (
            local.date().isoformat()
            if local.strftime("%H:%M") >= self.settings.daily_time
            else None
        )

    def run(self, *, scheduled: bool = False) -> dict:
        with exclusive(self.settings.state_dir, self.settings.allow_overlapping_runs):
            store = Store(self.settings.state_dir)
            if scheduled and not self.settings.enabled:
                return {"status": "disabled"}
            # Reconcile existing PRs on every minute-level tick, even after
            # today's scheduled development run has already started.
            recovered = self._recover(store)
            day = self.due_date() if scheduled else None
            if scheduled and (day is None or store.scheduled(day)):
                return {"status": "not_due_or_already_started", "recovered": recovered}
            run = uuid.uuid4().hex
            if not store.start(run, day):
                return {"status": "already_started"}
            with audit_commands(lambda kind, detail: store.event(run, None, kind, detail)):
                snapshot = self.github.snapshot()
                overview = self.github.overview()
            candidates = eligible(
                snapshot, allowlist=self.settings.issue_allowlist, pathmap=self.settings.issue_paths
            )
            # Only run tasks with an explicit, reviewed test command. Unconfigured
            # backlog remains visible to planning but cannot be silently certified.
            configured = [task for task in candidates if task.issue in self.settings.issue_checks]
            selected = assign(
                configured, self.settings.max_workers, self.settings.max_tasks_per_run
            )
            if selected:
                agent = self.agent_factory(
                    self.settings, lambda kind, detail: store.event(run, None, kind, detail)
                )
                plan = agent.run(
                    "planner",
                    json.dumps(
                        {
                            "eligible": [
                                {
                                    "issue": t.issue,
                                    "title": t.title,
                                    "priority": t.priority,
                                    "paths": sorted(t.paths),
                                    "target": t.target,
                                }
                                for t in configured
                            ],
                            "selected_by_conflict_rules": [t.issue for t in selected],
                            "repository": snapshot["repository"],
                            "open_prs": overview["open_prs"],
                            "recent_runs": overview["recent_runs"],
                            "recent_commits": [
                                {"sha": c["sha"]} for c in overview["recent_commits"]
                            ],
                            "branches": [b["name"] for page in overview["branches"] for b in page],
                        }
                    ),
                    self.settings.root,
                )
                approved = set(plan.get("selected", []))
                selected = [task for task in selected if task.issue in approved]
                store.event(
                    run,
                    None,
                    "plan",
                    {
                        "selected": [t.issue for t in selected],
                        "reason": str(plan.get("reason", ""))[:1000],
                    },
                )
            results = []
            with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
                futures = [pool.submit(self._task, run, task) for task in selected]
                for future in as_completed(futures):
                    results.append(future.result())
            store.finish(run)
            return {
                "run": run,
                "recovered": recovered,
                "results": results,
                "eligible": len(candidates),
                "configured": len(configured),
            }

    def _recover(self, store: Store) -> list[dict]:
        results = []
        for task in store.pending():
            if task["phase"] == "ci_wait" and task["pr"]:
                with audit_commands(
                    lambda kind, detail, run_id=task["run_id"], issue_id=task["issue"]: store.event(
                        run_id, issue_id, kind, detail
                    )
                ):
                    results.append(self._check_pr(store, task["run_id"], task["issue"], task["pr"]))
            else:
                # A killed edit/commit may have left side effects. Retain its
                # worktree and audit trail for reconciliation; do not replay it.
                store.phase(
                    task["run_id"],
                    task["issue"],
                    "blocked",
                    failure="Interrupted before PR; inspect worktree and remote state before retry",
                )
                results.append({"issue": task["issue"], "status": "blocked_after_interruption"})
        return results

    def _task(self, run: str, task: Assignment) -> dict:
        store = Store(self.settings.state_dir)
        with audit_commands(lambda kind, detail: store.event(run, task.issue, kind, detail)):
            return self._task_with_store(run, task, store)

    def _task_with_store(self, run: str, task: Assignment, store: Store) -> dict:
        branch = ""
        try:
            with self.mutation_lock:
                branch, worktree = self.github.prepare_worktree(task.issue, run)
                store.add_task(run, task.issue, task.target, branch, worktree)
                owner = self.github.login()
                self.github.transition(
                    task.target, task.issue, "claim", {"owner": owner, "branch": branch}
                )
                fresh = self.github.snapshot()
            claimed = next(
                (
                    item
                    for item in fresh["items"]
                    if item.get("issue") and item["issue"]["number"] == task.issue
                ),
                None,
            )
            if not claimed or claimed["fields"].get("Readiness") != "In Progress":
                raise RuntimeError(
                    "Claim did not persist in Project; stop for coordinator reconciliation"
                )
            self._setup(worktree, store, run, task.issue)
            feedback = ""
            agent = self.agent_factory(
                self.settings, lambda kind, detail: store.event(run, task.issue, kind, detail)
            )
            for _ in range(self.settings.max_review_cycles):
                cycle = store.cycle(run, task.issue)
                store.phase(run, task.issue, "developing")
                developer = agent.run(
                    "developer",
                    json.dumps(
                        {
                            "issue": task.issue,
                            "title": task.title,
                            "contract": task.body,
                            "target": task.target,
                            "branch": branch,
                            "review_cycle": cycle,
                            "previous_feedback": feedback,
                            "instruction": "Implement the complete issue contract; edit only this worktree.",
                        }
                    ),
                    worktree,
                    editable=True,
                )
                if developer.get("blocker"):
                    raise RuntimeError(f"Developer blocker: {str(developer['blocker'])[:500]}")
                missing = [
                    key
                    for key in (
                        "summary",
                        "decisions",
                        "risks",
                        "compatibility",
                        "migrations",
                        "documentation",
                        "deployment",
                    )
                    if not str(developer.get(key, "")).strip()
                ]
                if missing:
                    feedback = "Developer handoff omitted required PR context: " + ", ".join(
                        missing
                    )
                    store.event(run, task.issue, "handoff_gap", {"missing": missing})
                    continue
                changed = self.github.changed(worktree)
                if not changed:
                    raise RuntimeError("Developer produced no changed files")
                if task.paths and any(
                    not self._in_owned_paths(path, task.paths) for path in changed
                ):
                    raise RuntimeError(
                        "Developer changed a path outside the reviewed task ownership"
                    )
                store.event(
                    run,
                    task.issue,
                    "developer_result",
                    {"changed": changed, "summary": str(developer.get("summary", ""))[:1000]},
                )
                store.phase(run, task.issue, "reviewing")
                review = agent.run(
                    "reviewer",
                    json.dumps(
                        {
                            "issue": task.issue,
                            "contract": task.body,
                            "changed": changed,
                            "diff": self._diff(worktree),
                            "cycle": cycle,
                            "instruction": "Review the complete diff, relevant runtime paths and tests.",
                        }
                    ),
                    worktree,
                )
                store.event(run, task.issue, "review_cycle", {"cycle": cycle, "result": review})
                if not review.get("approved") or review.get("findings"):
                    feedback = "Review findings: " + json.dumps(review.get("findings", []))
                    continue
                store.phase(run, task.issue, "qa")
                evidence = self._checks(task.issue, worktree)
                store.event(run, task.issue, "qa_commands", evidence)
                if any(not entry["passed"] for entry in evidence):
                    feedback = "QA command failures: " + json.dumps(evidence)
                    continue
                qa = agent.run(
                    "qa",
                    json.dumps(
                        {
                            "issue": task.issue,
                            "contract": task.body,
                            "changed": changed,
                            "commands": evidence,
                            "instruction": "Verify the issue-specific acceptance and coverage evidence.",
                        }
                    ),
                    worktree,
                )
                store.event(run, task.issue, "qa_verdict", qa)
                if not qa.get("approved") or qa.get("gaps"):
                    feedback = "QA gaps: " + json.dumps(qa.get("gaps", []))
                    continue
                store.phase(run, task.issue, "pr_open")
                with self.mutation_lock:
                    head = self.github.commit_and_push(worktree, branch, task.issue)
                    store.head(run, task.issue, head)
                    body = self._pr_body(
                        task, head, evidence, cycle, review, qa, developer, changed
                    )
                    pr = self.github.create_pr(
                        worktree, f"feat: implement #{task.issue} — {task.title}", body
                    )
                store.pr(run, task.issue, pr)
                with self.mutation_lock:
                    self.github.transition(
                        task.target,
                        task.issue,
                        "review",
                        {
                            "evidence": [
                                f"PR #{pr} at {head}; review cycle {cycle}; QA commands passed"
                            ]
                        },
                    )
                    reviewers = self.github.request_reviewers(worktree, pr, owner)
                    self.github.record_reviewers(pr, reviewers)
                store.event(run, task.issue, "reviewer_requests", reviewers)
                store.phase(run, task.issue, "ci_wait")
                return self._check_pr(store, run, task.issue, pr)
            raise RuntimeError(
                f"Review/QA did not pass within {self.settings.max_review_cycles} cycles: {feedback[:800]}"
            )
        except Exception as error:  # noqa: BLE001 - preserve failed task for reconciliation
            if branch:
                try:
                    store.phase(run, task.issue, "blocked", failure=str(error)[:1000])
                except KeyError:
                    store.event(run, task.issue, "unrecorded_failure", str(error)[:1000])
            else:
                store.event(run, task.issue, "worktree_failure", str(error)[:1000])
            return {"issue": task.issue, "status": "blocked", "error": str(error)[:500]}

    def _diff(self, worktree: Path) -> str:
        patch = command(
            ["git", "diff", "--binary", f"origin/{self.settings.base_branch}"], worktree
        )
        for path in self.github.changed(worktree):
            candidate = worktree / path
            if candidate.is_file() and path not in patch and candidate.stat().st_size < 100_000:
                patch += f"\nNew file {path}:\n" + candidate.read_text(errors="replace")
        if len(patch) > 250_000:
            raise RuntimeError("Diff exceeds reviewer context limit; split task before review")
        return patch

    @staticmethod
    def _in_owned_paths(path: str, owned: frozenset[str]) -> bool:
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in owned)

    def _checks(self, issue: int, worktree: Path) -> list[dict]:
        commands = [*self.settings.base_checks, *self.settings.issue_checks[issue]]
        if not any(self._coverage_command(item) for item in self.settings.issue_checks[issue]):
            return [
                {
                    "command": "coverage",
                    "passed": False,
                    "output": "Issue-specific pytest coverage command with --cov-fail-under=95 or higher is required",
                }
            ]
        results = []
        for item in commands:
            args = shlex.split(item)
            if not args or any(token in {";", "&&", "|", ">"} for token in args):
                results.append({"command": item, "passed": False, "output": "Invalid command"})
                break
            try:
                proc = subprocess.run(
                    args, cwd=worktree, text=True, capture_output=True, check=False, timeout=3600
                )
                results.append(
                    {
                        "command": item,
                        "passed": proc.returncode == 0,
                        "exit_code": proc.returncode,
                        "output": (proc.stdout + proc.stderr)[-3000:],
                    }
                )
                if args[0].endswith("ci-local.sh") and re.search(
                    r"^SKIP\s", proc.stdout + proc.stderr, re.MULTILINE
                ):
                    results[-1]["passed"] = False
                    results[-1]["output"] += "\nRequired CI parity job was skipped"
                if self._coverage_command(item):
                    match = re.search(
                        r"^TOTAL\s+.*?\s+(\d+(?:\.\d+)?%)\s*$",
                        proc.stdout + proc.stderr,
                        re.MULTILINE,
                    )
                    results[-1]["coverage"] = match.group(1) if match else None
                    if not match:
                        results[-1]["passed"] = False
                        results[-1]["output"] += "\nCoverage summary missing"
            except (OSError, subprocess.TimeoutExpired) as error:
                results.append({"command": item, "passed": False, "output": str(error)[:500]})
            if not results[-1]["passed"]:
                break
        return results

    def _setup(self, worktree: Path, store: Store, run: str, issue: int) -> None:
        if (worktree / "caliber/.venv/bin/python").exists():
            return
        for item in self.settings.setup_commands:
            args = shlex.split(item)
            if not args or any(token in {";", "&&", "|", ">"} for token in args):
                raise RuntimeError(f"Invalid setup command: {item}")
            proc = subprocess.run(
                args, cwd=worktree, text=True, capture_output=True, check=False, timeout=3600
            )
            store.event(
                run,
                issue,
                "setup_command",
                {
                    "command": item,
                    "exit_code": proc.returncode,
                    "output": (proc.stdout + proc.stderr)[-1000:],
                },
            )
            if proc.returncode:
                raise RuntimeError(f"Setup failed: {item} (exit {proc.returncode})")

    @staticmethod
    def _coverage_command(item: str) -> bool:
        try:
            args = shlex.split(item)
        except ValueError:
            return False
        invokes_pytest = "pytest" in args or any(
            args[index : index + 2] == ["-m", "pytest"] for index in range(len(args) - 1)
        )
        if not invokes_pytest or not any(
            arg == "--cov" or arg.startswith("--cov=") for arg in args
        ):
            return False
        for index, arg in enumerate(args):
            if arg.startswith("--cov-fail-under="):
                value = arg.split("=", 1)[1]
            elif arg == "--cov-fail-under" and index + 1 < len(args):
                value = args[index + 1]
            else:
                continue
            try:
                return float(value) >= 95
            except ValueError:
                return False
        return False

    @staticmethod
    def _pr_body(
        task: Assignment,
        head: str,
        evidence: list[dict],
        cycle: int,
        review: dict,
        qa: dict,
        developer: dict,
        changed: list[str],
    ) -> str:
        tests = "\n".join(
            f"- `{row['command']}` — {'PASS' if row['passed'] else 'FAIL'}"
            + (f"; measured coverage {row['coverage']}" if row.get("coverage") else "")
            for row in evidence
        )
        files = "\n".join(f"- `{path}`" for path in changed)
        return f"""## Summary

Implements #{task.issue}: {task.title}. The live Project issue supplies the complete acceptance contract. This PR keeps the owner as final merger.

Goal and motivation: {task.title}

Project target: {task.target}.

Implementation: {developer.get("summary", "See changed files below.")}

Changed files:

{files}

Design decisions: {developer.get("decisions", "Followed the issue contract and repository policy.")}

Candidate: `{head}`. Closes #{task.issue} after owner merge.

## Tests and validation

- [x] Added or updated tests for changed behavior; QA evaluated the issue-specific evidence.
- [x] Ran configured CI-equivalent checks for affected areas; see exact commands below.
- [x] Ran `git diff --check`.
- [x] Checked for secrets, unrelated changes, generated docs/assets, migrations, and deployment implications in review.

Validation commands and results:

{tests}

Review cycle {cycle}: {json.dumps(review, sort_keys=True)}

QA verdict: {json.dumps(qa, sort_keys=True)}

## Risks and follow-up

Risks and limitations: {developer.get("risks", "Owner review and exact-head required CI remain pending when this PR opens.")}

Compatibility: {developer.get("compatibility", "See issue contract.")}

Migrations: {developer.get("migrations", "See issue contract.")}

Documentation: {developer.get("documentation", "See changed files.")}

Deployment: {developer.get("deployment", "No deployment performed by this workflow.")}

The owner controls final merge.

## Copilot handoff

- [ ] This PR is ready for review only after all applicable validation passes; merge requires the gates in `AGENTS.md`.
- [x] Reviewer status is reported accurately; model self-review is not independent GitHub approval.
"""

    def _check_pr(self, store: Store, run: str, issue: int, pr: int) -> dict:
        try:
            state = self.github.pr_state(pr)
            task = store.task(run, issue)
            expected = task["head"]
            if (
                not expected
                or state["headRefOid"] != expected
                or state["headRefName"] != task["branch"]
                or state["baseRefName"] != self.settings.base_branch
            ):
                store.phase(
                    run,
                    issue,
                    "blocked",
                    failure="PR head or branch changed after review/QA; revalidate the new candidate",
                )
                return {"issue": issue, "status": "blocked_changed_head", "pr": state["url"]}
            if state.get("isDraft") is False and store.task(run, issue)["phase"] == "ready":
                return {"issue": issue, "status": "ready", "pr": state["url"]}
            owner = self.settings.repository.split("/")[0]
            author = state.get("author", {}).get("login", "")
            checks = self.github.required_checks(pr, state)
            unresolved = self.github.unresolved_threads(pr)
            ready, reasons = pr_ready(state, checks, unresolved, owner, author)
            store.event(
                run,
                issue,
                "pr_readiness",
                {
                    "head": state["headRefOid"],
                    "checks": checks,
                    "unresolved": unresolved,
                    "reasons": reasons,
                },
            )
            if ready:
                self.github.mark_ready_body(pr, checks)
                if state["isDraft"]:
                    command(
                        ["gh", "pr", "ready", str(pr), "--repo", self.settings.repository],
                        self.settings.root,
                    )
                store.phase(run, issue, "ready")
                return {"issue": issue, "status": "ready_for_owner", "pr": state["url"]}
            bad_checks = [row for row in checks if row["state"] in {"FAIL", "SKIPPING"}]
            changes_requested = any(
                review.get("state") == "CHANGES_REQUESTED"
                for review in state.get("latestReviews", [])
            )
            started = store.phase_started(run, issue, "ci_wait")
            stalled = bool(
                started
                and (datetime.now(UTC) - started).total_seconds() > self.settings.ci_timeout_seconds
            )
            if bad_checks or changes_requested or stalled:
                reason = (
                    f"PR #{pr} needs same-branch correction: failed/skipped checks="
                    f"{[row['name'] for row in bad_checks]}, changes_requested={changes_requested}, "
                    f"CI stalled={stalled}; exact head {expected}"
                )
                store.phase(run, issue, "blocked", failure=reason)
                try:
                    self.github.transition(task["target"], issue, "blocked", {"reason": reason})
                except Exception as error:  # noqa: BLE001 - local block remains authoritative for this run
                    store.event(run, issue, "remote_block_error", str(error)[:500])
                return {
                    "issue": issue,
                    "status": "blocked_ci_or_review",
                    "pr": state["url"],
                    "reasons": reasons,
                }
            return {
                "issue": issue,
                "status": "waiting_for_ci_or_review",
                "pr": state["url"],
                "reasons": reasons,
            }
        except Exception as error:  # noqa: BLE001 - preserve PR wait state and diagnostics
            store.event(run, issue, "pr_check_error", str(error)[:500])
            return {"issue": issue, "status": "pr_check_error", "error": str(error)[:500]}
