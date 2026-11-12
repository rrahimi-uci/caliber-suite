"""The planning, development, review, QA, merge and Project Done state machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import credential_values, make_agent, untrusted_env, worker_args
from .config import Settings
from .diagnostics import failure_code
from .github import GitHub, audit_commands, command, pr_ready
from .planner import (
    Assignment,
    DecisionCandidate,
    assign,
    completed_today,
    decision_candidates,
    eligible,
    view_label,
)
from .state import LockBusyError, Store, exclusive

# Terminal colour codes in check output; stripped so labels and summaries parse.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Strips trailing spaces/tabs and surplus final blank lines from changed UTF-8 text files,
# the two things ``git diff --check`` rejects that a shell-less developer cannot see.
_WHITESPACE_SCRIPT = """
import sys
from pathlib import Path
for name in sys.argv[1:]:
    path = Path(name)
    data = path.read_bytes()
    if b"\\0" in data:
        continue
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        continue
    lines = [line.rstrip(" \\t") for line in text.split("\\n")]
    fixed = "\\n".join(lines).rstrip("\\n")
    fixed = fixed + "\\n" if fixed else ""
    if fixed != text:
        path.write_text(fixed, encoding="utf-8")
"""


# Failures a fresh attempt can plausibly fix. A developer blocker, scope or protected-path
# violation, interruption or an unclassified failure still needs a human.
_AUTO_RETRY_CODES = {
    "review_qa_exhausted",
    "issue_qa_missing",
    "no_changes",
    "setup_failed",
    "docs_sync_failed",
    "budget_exhausted",
    "budget_unverifiable",
}


def _auto_retryable(code: str) -> bool:
    return code in _AUTO_RETRY_CODES or code.startswith(
        ("agent_", "reviewer_", "qa_output", "qa_error")
    )


class Engine:
    def __init__(
        self, settings: Settings, *, github: GitHub | None = None, agent_factory=make_agent
    ):
        self.settings = settings
        self.github = github or GitHub(settings)
        self.agent_factory = agent_factory
        self.mutation_lock = threading.Lock()
        self.model_lock = threading.Lock()
        self._agent_cache: dict[tuple[str, int | None], object] = {}

    def due_date(self, now: datetime | None = None) -> str | None:
        local = (now or datetime.now(ZoneInfo(self.settings.timezone))).astimezone(
            ZoneInfo(self.settings.timezone)
        )
        return (
            local.date().isoformat()
            if local.strftime("%H:%M") >= self.settings.daily_time
            else None
        )

    def run(
        self,
        *,
        scheduled: bool = False,
        exclude_issues: frozenset[int] = frozenset(),
        retry_issue: int | None = None,
        auto_retry: bool = False,
    ) -> dict:
        if retry_issue is not None and (
            scheduled or type(retry_issue) is not int or retry_issue <= 0
        ):
            raise ValueError("A retry issue must be a positive number in a manual run")
        try:
            return self._run_locked(
                scheduled=scheduled,
                exclude_issues=exclude_issues,
                retry_issue=retry_issue,
                auto_retry=auto_retry,
            )
        except LockBusyError:
            if scheduled:
                return {"status": "busy"}
            raise

    def monitor(self) -> dict:
        """Reconcile published PRs without starting another development run."""
        with exclusive(self.settings.state_dir, self.settings.allow_overlapping_runs):
            store = Store(self.settings.state_dir)
            recovered = self._recover(store)
            return {
                "recovered": recovered,
                "pending": len(store.active()),
                "awaiting_owner": len(store.pending()) - len(store.active()),
            }

    def _run_locked(
        self,
        *,
        scheduled: bool,
        exclude_issues: frozenset[int],
        retry_issue: int | None,
        auto_retry: bool = False,
    ) -> dict:
        with exclusive(self.settings.state_dir, self.settings.allow_overlapping_runs):
            store = Store(self.settings.state_dir)
            if scheduled and not self.settings.enabled:
                return {"status": "disabled"}
            # Reconcile existing PRs on every minute-level tick, even after
            # today's scheduled development run has already started.
            recovered = self._recover(store)
            recovered.extend(self._recover_merged_blocked(store))
            if store.active():
                if retry_issue is not None:
                    raise RuntimeError(
                        "Cannot retry while a published PR is pending reconciliation"
                    )
                return {"status": "waiting_on_published_prs", "recovered": recovered}
            schedule_day = self.due_date() if scheduled else None
            if scheduled and (schedule_day is None or store.scheduled(schedule_day)):
                return {"status": "not_due_or_already_started", "recovered": recovered}
            run = uuid.uuid4().hex
            if not store.start(run, schedule_day):
                return {"status": "already_started"}
            try:
                return self._execute_run(
                    store, run, schedule_day, recovered, exclude_issues, retry_issue, auto_retry
                )
            except Exception as error:
                store.fail(run, str(error)[:500])
                raise

    def _execute_run(
        self,
        store: Store,
        run: str,
        schedule_day: str | None,
        recovered: list[dict],
        exclude_issues: frozenset[int],
        retry_issue: int | None,
        auto_retry: bool = False,
    ) -> dict:
        # A scheduled run's refill batches keep the day's automatic retries.
        reconcile_day = schedule_day is not None or auto_retry
        with audit_commands(lambda kind, detail: store.event(run, None, kind, detail)):
            snapshot = self.github.snapshot()
            overview = self.github.overview()
            label = view_label(snapshot, self.settings.project_view_number)
            day = schedule_day or datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
            done = self._daily_done(snapshot, label, day)
        midnight = datetime.combine(
            date.fromisoformat(day), time.min, ZoneInfo(self.settings.timezone)
        )
        attempted_today = store.attempted_since(midnight)
        branches = {branch["name"] for page in overview["branches"] for branch in page}
        if (
            reconcile_day
            and self.settings.max_auto_retries > 0
            and self._auto_reconcile(store, run, snapshot, branches, midnight)
        ):
            with audit_commands(lambda kind, detail: store.event(run, None, kind, detail)):
                snapshot = self.github.snapshot()
        proposal_candidates = decision_candidates(snapshot, scope_label=label)
        decision_proposals = self._run_decision_proposals(store, run, proposal_candidates)
        candidates = eligible(
            snapshot,
            allowlist=self.settings.issue_allowlist,
            pathmap=self.settings.issue_paths,
            scope_label=label,
            require_owned_paths=self.settings.require_owned_paths,
        )

        def retryable_previous(issue: int):
            previous = store.latest_task(issue)
            if (
                previous is None
                or previous["phase"] not in {"blocked", "failed"}
                or previous["pr"] is not None
                or previous["head"] is not None
                or not previous["branch"]
                or previous["branch"] in branches
            ):
                return None
            return previous

        blocked = store.blocked_issues()
        retry_branches: dict[int, str] = {}
        scheduled_retries = {}
        if retry_issue is not None:
            previous = retryable_previous(retry_issue)
            retry_task = next((task for task in candidates if task.issue == retry_issue), None)
            if previous is None or retry_task is None or retry_task.readiness != "Ready":
                raise RuntimeError(
                    "Retry requires a blocked pre-PR attempt, absent remote branch, and a freshly Ready unclaimed Project task"
                )
            candidates = [task for task in candidates if task.issue == retry_issue]
            retry_branches[retry_issue] = previous["branch"]
            store.event(run, retry_issue, "retry_authorized", {"prior_run": previous["run_id"]})
        elif reconcile_day:
            # A reviewed Todo/Ready Project transition can authorize a pre-PR
            # blocked task on the next Pacific day. Never replay a same-day
            # attempt, a published head/PR, or a branch that reappeared remotely.
            for task in candidates:
                if task.issue not in blocked or task.issue in attempted_today:
                    continue
                previous = retryable_previous(task.issue)
                if previous is None or previous["phase"] != "blocked" or task.readiness != "Ready":
                    continue
                blocked_at = store.phase_started(previous["run_id"], task.issue, "blocked")
                if blocked_at is not None and self.github.has_ready_reconciliation(
                    task.issue, blocked_at
                ):
                    retry_branches[task.issue] = previous["branch"]
                    scheduled_retries[task.issue] = previous["run_id"]
        explicit_retry = {retry_issue} if retry_issue is not None else set()
        excluded = (
            exclude_issues | (blocked - retry_branches.keys()) | (attempted_today - explicit_retry)
        )
        candidates = [task for task in candidates if task.issue not in excluded]
        # Explicit commands augment the repository gate. The fallback runs the
        # full backend suite with coverage for every changed production file.
        configured = [task for task in candidates if task.issue in self.settings.issue_checks]
        remaining = max(0, self.settings.daily_done_target - len(done))
        remaining_attempts = max(
            0, self.settings.max_tasks_per_run - store.attempt_count_since(midnight)
        )
        selected = assign(
            candidates,
            self.settings.max_workers,
            min(remaining_attempts, remaining),
        )
        if selected:
            plan = self._run_agent(
                store,
                run,
                None,
                "planner",
                json.dumps(
                    {
                        "eligible": [
                            {
                                "issue": t.issue,
                                "title": t.title,
                                "priority": t.priority,
                                "execution_order": t.execution_order,
                                "readiness": t.readiness,
                                "paths": sorted(t.paths),
                                "target": t.target,
                            }
                            for t in candidates
                        ],
                        "selected_by_conflict_rules": [t.issue for t in selected],
                        "repository": snapshot["repository"],
                        "open_prs": overview["open_prs"],
                        "recent_runs": overview["recent_runs"],
                        "recent_commits": [{"sha": c["sha"]} for c in overview["recent_commits"]],
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
            for task in selected:
                if task.issue in scheduled_retries:
                    store.event(
                        run,
                        task.issue,
                        "retry_authorized",
                        {
                            "prior_run": scheduled_retries[task.issue],
                            "source": "scheduled_reconciled_ready",
                        },
                    )
        if retry_issue is not None and not selected:
            raise RuntimeError("Explicit retry was not selected by planner or daily attempt cap")
        results = []
        with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
            futures = [
                pool.submit(
                    self._task,
                    run,
                    task,
                    *([retry_branches[task.issue]] if task.issue in retry_branches else []),
                )
                for task in selected
            ]
            for future in as_completed(futures):
                results.append(future.result())
        if results:
            with audit_commands(lambda kind, detail: store.event(run, None, kind, detail)):
                done = self._daily_done(self.github.snapshot(), label, day)
            remaining = max(0, self.settings.daily_done_target - len(done))
        proposal_errors = [
            item for item in decision_proposals if item.get("status") == "proposal_error"
        ]
        if selected:
            status = "planned_with_decision_proposal_errors" if proposal_errors else "planned"
        elif proposal_errors:
            status = "decision_proposal_error"
        elif decision_proposals:
            status = "decision_proposals_ready"
        else:
            status = "no_executable_tasks"
        store.finish(run)
        return {
            "run": run,
            "status": status,
            "recovered": recovered,
            "results": results,
            "eligible": len(candidates),
            "configured": len(configured),
            "selected": [task.issue for task in selected],
            "decision_candidates": [task.issue for task in proposal_candidates],
            "decision_proposals": decision_proposals,
            "view": snapshot["view"],
            "daily_done_target": self.settings.daily_done_target,
            "done_today": done,
            "remaining_to_target": remaining,
        }

    @staticmethod
    def _decision_fingerprint(candidate: DecisionCandidate) -> str:
        payload = {
            "issue": candidate.issue,
            "title": candidate.title,
            "body": candidate.body,
            "target": candidate.target,
            "priority": candidate.priority,
            "execution_order": candidate.execution_order,
            "readiness": candidate.readiness,
            "human_input": candidate.human_input,
            "dependencies": candidate.dependencies,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _decision_gate(human_input: str) -> str:
        gates = [
            part.strip()
            for part in human_input.split(",")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,20}\s*:\s*start", part.strip())
        ]
        return ", ".join(gates[:4]) if gates else "explicit start gate"

    def _run_decision_proposals(
        self,
        store: Store,
        run: str,
        candidates: list[DecisionCandidate],
    ) -> list[dict]:
        """Prepare bounded decision packets without changing governed task state."""
        summaries: list[dict] = []
        limit = self.settings.max_decision_proposals_per_run
        for candidate in candidates[:limit]:
            fingerprint = self._decision_fingerprint(candidate)
            if store.has_event_fingerprint(candidate.issue, "decision_proposal", fingerprint):
                continue
            prompt = json.dumps(
                {
                    "repository": self.settings.repository,
                    "issue": candidate.issue,
                    "title": candidate.title,
                    "body": candidate.body,
                    "target": candidate.target,
                    "priority": candidate.priority,
                    "execution_order": candidate.execution_order,
                    "readiness": candidate.readiness,
                    "human_input": candidate.human_input,
                    "dependencies": candidate.dependencies,
                },
                sort_keys=True,
            )
            try:
                packet = self._run_agent(
                    store,
                    run,
                    candidate.issue,
                    "decision",
                    prompt,
                    self.settings.root,
                    editable=False,
                )
            except Exception as error:  # noqa: BLE001 - bounded public code is recorded below
                code = failure_code(str(error))
                store.event(
                    run,
                    candidate.issue,
                    "decision_proposal_error",
                    {
                        "fingerprint": fingerprint,
                        "gate": self._decision_gate(candidate.human_input),
                        "failure_code": code,
                    },
                )
                summaries.append(
                    {
                        "issue": candidate.issue,
                        "status": "proposal_error",
                        "gate": self._decision_gate(candidate.human_input),
                    }
                )
                continue
            store.event(
                run,
                candidate.issue,
                "decision_proposal",
                {
                    "fingerprint": fingerprint,
                    "gate": self._decision_gate(candidate.human_input),
                    "packet": packet,
                },
            )
            summaries.append(
                {
                    "issue": candidate.issue,
                    "status": "proposal_ready",
                    "gate": self._decision_gate(candidate.human_input),
                    "option_count": len(packet["options"]),
                }
            )
        return summaries

    def _daily_done(self, snapshot: dict, label: str, day: str) -> list[int]:
        midnight = datetime.combine(
            date.fromisoformat(day), time.min, ZoneInfo(self.settings.timezone)
        )
        since = midnight.astimezone(UTC).date().isoformat()
        return completed_today(
            snapshot,
            self.github.closed_issues_since(since, label),
            label,
            day,
            self.settings.timezone,
        )

    def _recover(self, store: Store) -> list[dict]:
        results = []
        for task in store.pending():
            if task["phase"] in {"ci_wait", "ready", "merged_unreconciled"} and task["pr"]:
                with audit_commands(
                    lambda kind, detail, run_id=task["run_id"], issue_id=task["issue"]: store.event(
                        run_id, issue_id, kind, detail
                    )
                ):
                    results.append(self._check_pr(store, task["run_id"], task["issue"], task["pr"]))
            else:
                # A killed edit/commit may have left side effects. Retain its
                # worktree and audit trail for reconciliation; do not replay it.
                reason = "Interrupted before PR; inspect worktree and remote state before retry"
                store.phase(task["run_id"], task["issue"], "blocked", failure=reason)
                # Keep Project truthful: the claim otherwise stays In Progress forever.
                try:
                    with self.mutation_lock:
                        self.github.transition(
                            task["target"], task["issue"], "blocked", {"reason": reason}
                        )
                except Exception as error:  # noqa: BLE001 - local block remains durable
                    store.event(
                        task["run_id"], task["issue"], "remote_block_error", str(error)[:500]
                    )
                results.append({"issue": task["issue"], "status": "blocked_after_interruption"})
        return results

    def _recover_merged_blocked(self, store: Store) -> list[dict]:
        """Complete a blocked task whose PR the owner fixed and merged anyway.

        CI failures, requested changes or an owner push block a published task, but
        the owner may still merge its PR; that merge must reach Project Done.
        """
        results = []
        for task in store.blocked_with_pr():
            try:
                state = self.github.pr_state(task["pr"])
            except Exception as error:  # noqa: BLE001 - checked again on the next run
                store.event(task["run_id"], task["issue"], "pr_check_error", str(error)[:500])
                continue
            if state.get("state") == "MERGED":
                with audit_commands(
                    lambda kind, detail, run_id=task["run_id"], issue_id=task["issue"]: store.event(
                        run_id, issue_id, kind, detail
                    )
                ):
                    results.append(
                        self._reconcile_merged(
                            store, task["run_id"], task["issue"], task["pr"], state
                        )
                    )
        return results

    def _task(self, run: str, task: Assignment, retry_branch: str | None = None) -> dict:
        store = Store(self.settings.state_dir)
        with audit_commands(lambda kind, detail: store.event(run, task.issue, kind, detail)):
            return self._task_with_store(run, task, store, retry_branch)

    def _ensure_ready(self, task: Assignment) -> None:
        """Promote a dependency-ready Planned task through the governed Project transition."""
        if task.readiness != "Planned":
            return
        order = (
            str(task.execution_order)
            if task.execution_order is not None
            else "not assigned; using priority/issue fallback"
        )
        self.github.transition(
            task.target,
            task.issue,
            "ready",
            {
                "evidence": [
                    f"Scheduler selected the earliest executable task at execution order {order}; all inspected native blockers are CLOSED/COMPLETED."
                ]
            },
        )

    def _auto_reconcile(
        self,
        store: Store,
        run: str,
        snapshot: dict,
        branches: set[str],
        midnight: datetime,
    ) -> list[int]:
        """Return cleanly blocked pre-PR tasks to Ready on a later Pacific day.

        This is the manual reconciliation the owner otherwise performs: verify the
        attempt published nothing (no head, PR or remote branch), drop the
        coordinator's own claim, and apply a reviewed Ready transition through the
        repository helper. Only failures a fresh attempt can plausibly fix qualify,
        and each issue gets at most ``max_auto_retries`` automatic returns.
        """
        owner = self.settings.repository.split("/", 1)[0]
        items = {item["issue"]["number"]: item for item in snapshot["items"] if item.get("issue")}
        reconciled = []
        for issue in sorted(store.blocked_issues()):
            previous = store.latest_task(issue)
            if (
                previous is None
                or previous["phase"] != "blocked"
                or previous["pr"] is not None
                or previous["head"] is not None
                or not previous["branch"]
                or previous["branch"] in branches
            ):
                continue
            code = failure_code(previous["failure"])
            if not _auto_retryable(code):
                continue
            blocked_at = store.phase_started(previous["run_id"], issue, "blocked")
            if blocked_at is None or blocked_at >= midnight:
                continue
            if store.event_count(issue, "auto_reconciled") >= self.settings.max_auto_retries:
                continue
            item = items.get(issue)
            if item is None:
                continue
            fields, details = item["fields"], item["issue"]
            assignees = details.get("assignees") or []
            if (
                details.get("state") != "OPEN"
                or fields.get("Readiness") != "Blocked"
                or any(login != owner for login in assignees)
                or any(pr.get("state") == "OPEN" for pr in details.get("prs") or [])
            ):
                continue
            try:
                if assignees or fields.get("Status") != "Todo":
                    self.github.release_claim(snapshot, issue, owner)
                self.github.transition(
                    previous["target"],
                    issue,
                    "ready",
                    {
                        "evidence": [
                            (
                                "Automatic next-day reconciliation of blocked attempt "
                                f"{previous['run_id'][:8]} (failure_code {code}). Verified by "
                                "the coordinator: no pushed head, no PR, remote branch "
                                f"{previous['branch']} absent, claim released."
                            )
                        ]
                    },
                )
            except Exception as error:  # noqa: BLE001 - leave the task blocked for a human
                store.event(run, issue, "auto_reconcile_error", str(error)[:500])
                continue
            store.event(
                run,
                issue,
                "auto_reconciled",
                {"prior_run": previous["run_id"], "failure_code": code},
            )
            reconciled.append(issue)
        return reconciled

    def _run_agent(
        self,
        store: Store,
        run: str,
        issue: int | None,
        role: str,
        prompt: str,
        cwd: Path,
        *,
        editable: bool = False,
    ) -> dict:
        """Run one model call under the Pacific-day aggregate spend guard."""
        with self.model_lock:
            day = datetime.now(ZoneInfo(self.settings.timezone)).date()
            midnight = datetime.combine(day, time.min, ZoneInfo(self.settings.timezone))
            spent, complete = store.model_spend_since(midnight)
            if not complete and self.settings.unknown_call_cost_usd > 0:
                # A call killed for a stall or timeout reports no cost. Charging it a
                # conservative estimate keeps the guard honest without stopping every
                # later call that day.
                spent += self.settings.unknown_call_cost_usd * store.unknown_cost_calls_since(
                    midnight
                )
                complete = True
            if self.settings.daily_budget_usd > 0:
                if not complete:
                    raise RuntimeError("Daily Claude spend cannot be verified; stopping safely")
                remaining = self.settings.daily_budget_usd - spent
                if remaining <= 0:
                    raise RuntimeError(
                        f"Daily Claude budget exhausted (${self.settings.daily_budget_usd:.2f})"
                    )
                call_budget = remaining
                if self.settings.max_budget_usd > 0:
                    call_budget = min(call_budget, self.settings.max_budget_usd)
            else:
                call_budget = self.settings.max_budget_usd
            key = (run, issue)
            agent = self._agent_cache.get(key)
            if agent is None:
                settings = replace(self.settings, max_budget_usd=call_budget)
                agent = self.agent_factory(
                    settings, lambda kind, detail: store.event(run, issue, kind, detail)
                )
                self._agent_cache[key] = agent
            elif hasattr(agent, "settings"):
                agent.settings = replace(agent.settings, max_budget_usd=call_budget)
            return agent.run(role, prompt, cwd, editable=editable)

    def _task_with_store(
        self, run: str, task: Assignment, store: Store, retry_branch: str | None = None
    ) -> dict:
        branch = ""
        try:
            with self.mutation_lock:
                if retry_branch and self.github.remote_branch_exists(retry_branch):
                    raise RuntimeError("Previously blocked branch appeared remotely before retry")
                self._ensure_ready(task)
                branch, worktree = self.github.prepare_worktree(task.issue, run)
                store.add_task(run, task.issue, task.target, branch, worktree)
                claim_actor = self.github.login()
                self.github.transition(
                    task.target, task.issue, "claim", {"owner": claim_actor, "branch": branch}
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
            for _ in range(self.settings.max_review_cycles):
                cycle = store.cycle(run, task.issue)
                # What this cycle set out to fix; the reviewer rechecks it.
                cycle_feedback = feedback
                evidence: list[dict] | None = None
                # Fix loop: the developer has no shell, so the coordinator runs every
                # gate and hands failures straight back. Only a candidate whose gates
                # pass (or whose fix attempts ran out) costs a reviewer call and a cycle.
                for attempt in range(self.settings.max_fix_attempts + 1):
                    store.phase(run, task.issue, "developing")
                    developer = self._run_agent(
                        store,
                        run,
                        task.issue,
                        "developer",
                        json.dumps(
                            {
                                "issue": task.issue,
                                "title": task.title,
                                "contract": task.body,
                                "target": task.target,
                                "branch": branch,
                                "review_cycle": cycle,
                                "fix_attempt": attempt,
                                "previous_feedback": feedback,
                                "instruction": "Implement the complete issue contract; edit only this worktree."
                                if attempt == 0
                                else "Fix every failing coordinator check in previous_feedback; keep the contract complete.",
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
                            "tests_suggested",
                            "tests_run",
                            "remaining_work",
                        )
                        if not str(developer.get(key, "")).strip()
                    ]
                    if missing:
                        feedback = "Developer handoff omitted required PR context: " + ", ".join(
                            missing
                        )
                        store.event(run, task.issue, "handoff_gap", {"missing": missing})
                        evidence = None
                        continue
                    changed = self.github.changed(worktree)
                    if not changed:
                        raise RuntimeError("Developer produced no changed files")
                    self._validate_task_paths(changed, task.paths)
                    self._normalize(worktree, changed, store, run, task.issue)
                    changed = self.github.changed(worktree)
                    self._validate_task_paths(changed, task.paths)
                    if any(self._docs_source_path(path) for path in changed):
                        self._sync_generated_docs(worktree, store, run, task.issue)
                        changed = self.github.changed(worktree)
                        self._validate_task_paths(changed, task.paths)
                    store.event(
                        run,
                        task.issue,
                        "developer_result",
                        {
                            "changed": changed,
                            "summary": str(developer.get("summary", ""))[:1000],
                            "fix_attempt": attempt,
                        },
                    )
                    store.phase(run, task.issue, "reviewing")
                    candidate_patch = self._diff(worktree)
                    evidence = self._checks(task.issue, worktree, changed)
                    store.event(run, task.issue, "qa_commands", evidence)
                    if (
                        self.github.changed(worktree) != changed
                        or self._diff(worktree) != candidate_patch
                    ):
                        raise RuntimeError(
                            "QA commands changed the candidate; rerun development and review"
                        )
                    failed_checks = [entry for entry in evidence if not entry["passed"]]
                    if not failed_checks:
                        break
                    feedback = "QA command failures: " + json.dumps(failed_checks)
                if evidence is None:
                    continue
                reviewed_patch = candidate_patch
                review_view = self._review_view(worktree, changed, reviewed_patch)
                review = self._run_agent(
                    store,
                    run,
                    task.issue,
                    "reviewer",
                    json.dumps(
                        {
                            "issue": task.issue,
                            "contract": task.body,
                            "changed": changed,
                            **review_view,
                            "commands": evidence,
                            "cycle": cycle,
                            "previous_feedback": cycle_feedback,
                            "instruction": "Review the full source diff, relevant runtime paths, tests, and local CI evidence. Generated artifacts omitted from the inline diff are listed with exact hashes and sizes; inspect their paths with Read when needed and verify their generator and QA evidence.",
                        }
                    ),
                    worktree,
                )
                store.event(run, task.issue, "review_cycle", {"cycle": cycle, "result": review})
                failed_checks = [entry for entry in evidence if not entry["passed"]]
                blocking = self._blocking_findings(review.get("findings"))
                if review.get("approved") is not True or blocking or failed_checks:
                    # The developer has no shell, so hand back the coordinator's own failing
                    # command output alongside the findings instead of only the reviewer's
                    # paraphrase of it.
                    parts = []
                    if review.get("approved") is not True or blocking:
                        listed = review.get("findings")
                        parts.append(
                            "Review findings: "
                            + json.dumps(blocking or (listed if isinstance(listed, list) else []))
                        )
                    if failed_checks:
                        parts.append("QA command failures: " + json.dumps(failed_checks))
                    feedback = "\n\n".join(parts)
                    continue
                store.phase(run, task.issue, "qa")
                qa = self._run_agent(
                    store,
                    run,
                    task.issue,
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
                if any(secret in reviewed_patch for secret in credential_values()):
                    # A model that could read a coordinator credential must never get
                    # it into a pushed commit; nothing has been published yet.
                    raise RuntimeError("Candidate contains a coordinator credential; not published")
                store.phase(run, task.issue, "pr_open")
                with self.mutation_lock:
                    head = self.github.commit_and_push(
                        worktree, branch, task.issue, reviewed_patch, changed
                    )
                    store.head(run, task.issue, head)
                    body = self._pr_body(
                        task, head, evidence, cycle, review, qa, developer, changed
                    )
                    pr = self.github.create_pr(
                        worktree, f"feat: implement #{task.issue} — {task.title}", body
                    )
                store.pr(run, task.issue, pr)
                # The PR exists now: from here on the task is only ever waited on,
                # never blocked for bookkeeping. Project/reviewer writes are retried
                # by _check_pr until they succeed (GitHub links a new PR to its issue
                # asynchronously, and transient API errors happen).
                store.phase(run, task.issue, "ci_wait")
                store.event(
                    run,
                    task.issue,
                    "publish_evidence",
                    {"evidence": f"PR #{pr} at {head}; review cycle {cycle}; QA commands passed"},
                )
                self._post_pr_bookkeeping(store, run, task.issue, pr)
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
                try:
                    with self.mutation_lock:
                        self.github.transition(
                            task.target,
                            task.issue,
                            "blocked",
                            {
                                "reason": f"Automated CD run {run[:8]} blocked during development or QA. Inspect restricted state and claim before retry."
                            },
                        )
                except Exception as transition_error:  # noqa: BLE001 - retain primary blocker
                    store.event(
                        run, task.issue, "blocked_transition_failed", str(transition_error)[:500]
                    )
            else:
                store.event(run, task.issue, "worktree_failure", str(error)[:1000])
            return {"issue": task.issue, "status": "blocked", "error": str(error)[:500]}

    def _diff(self, worktree: Path) -> str:
        # Intent-to-add makes every untracked file visible to git diff while
        # preserving the complete candidate for the later staging step.
        command(["git", "add", "-N", "--all"], worktree)
        patch = command(
            ["git", "diff", "--binary", f"origin/{self.settings.base_branch}"], worktree
        )
        if len(patch) > 64_000_000:
            raise RuntimeError("Candidate diff exceeds integrity comparison limit; split task")
        return patch

    def _review_view(self, worktree: Path, changed: list[str], patch: str) -> dict:
        """Keep the reviewer prompt bounded without weakening exact-patch checks."""
        digest = hashlib.sha256(patch.encode()).hexdigest()
        generated = [path for path in changed if self._generated_docs_path(path)]
        if len(patch) <= 250_000:
            source_patch = patch
        else:
            if not generated:
                raise RuntimeError("Source diff exceeds reviewer context limit; split task")
            source = [path for path in changed if path not in generated]
            source_patch = (
                command(
                    [
                        "git",
                        "diff",
                        "--binary",
                        f"origin/{self.settings.base_branch}",
                        "--",
                        *(f":(literal){path}" for path in source),
                    ],
                    worktree,
                )
                if source
                else ""
            )
            if len(source_patch) > 250_000:
                raise RuntimeError("Source diff exceeds reviewer context limit; split task")
        artifacts = []
        for path in generated:
            target = worktree / path
            if target.is_symlink():
                raise RuntimeError("Generated artifact is a symlink; refuse review")
            if target.is_file():
                with target.open("rb") as stream:
                    artifact_digest = hashlib.file_digest(stream, "sha256").hexdigest()
                artifacts.append(
                    {"path": path, "bytes": target.stat().st_size, "sha256": artifact_digest}
                )
            elif target.exists():
                raise RuntimeError("Generated artifact is not a regular file; refuse review")
            else:
                artifacts.append({"path": path, "deleted": True})
        return {
            "diff": source_patch,
            "candidate_diff_sha256": digest,
            "generated_artifacts": artifacts,
        }

    @staticmethod
    def _in_owned_paths(path: str, owned: frozenset[str]) -> bool:
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in owned)

    def _validate_task_paths(self, changed: list[str], owned: frozenset[str]) -> None:
        protected = [path for path in changed if self._protected_path(path)]
        if protected and self.settings.auto_merge:
            raise RuntimeError(
                "Unattended merge cannot change workflow or policy files: " + ", ".join(protected)
            )
        workflows = [path for path in changed if path.startswith(".github/workflows/")]
        if workflows:
            # The coordinator's token has no `workflow` scope, so GitHub would reject
            # the push only after the whole review and QA spend.
            raise RuntimeError(
                "Developer changed a path the coordinator cannot publish: " + ", ".join(workflows)
            )
        # Generated served docs follow from an owned docs/backend source change.
        if owned and any(
            not self._in_owned_paths(path, owned) and not self._generated_docs_path(path)
            for path in changed
        ):
            raise RuntimeError("Developer changed a path outside the reviewed task ownership")

    @staticmethod
    def _protected_path(path: str) -> bool:
        return path in {
            "AGENTS.md",
            "scripts/ci-local.sh",
            ".pre-commit-config.yaml",
        } or path.startswith((".github/", ".agents/", "automated-cd/"))

    @staticmethod
    def _standard_check_path(path: str) -> bool:
        if path in {"README.md", "CONTRIBUTING.md", "ARCHITECTURE.md"}:
            return True
        if Engine._generated_docs_path(path):
            return True
        if path.startswith(("docs/", "caliber/docs/")) and path.endswith((".md", ".rst")):
            return True
        return path.startswith(("caliber/src/", "caliber/tests/")) and path.endswith(".py")

    @staticmethod
    def _docs_source_path(path: str) -> bool:
        return (
            path == "ARCHITECTURE.md"
            or (path.startswith("docs/") and path.endswith(".md"))
            or (path.startswith("caliber/src/caliber/") and path.endswith(".py"))
            or path.startswith(("sdk/caliber-sdk/", "docs-site/cookbooks/"))
            or Engine._generated_docs_path(path)
        )

    @staticmethod
    def _generated_docs_path(path: str) -> bool:
        for prefix in ("docs-site/", "caliber/caliber-ui/public/docs/"):
            if path.startswith(prefix):
                name = path.removeprefix(prefix)
                return "/" not in name and (
                    (name.startswith("m-") and name.endswith((".html", ".md")))
                    or name in {"docs-nav.js", "search-index.json", "llms.txt"}
                )
        return False

    def _normalize(
        self, worktree: Path, changed: list[str], store: Store, run: str, issue: int
    ) -> None:
        """Apply the repository's mechanical formatting before the candidate is reviewed.

        The developer role has no shell, so it cannot run the formatter or see what
        ``git diff --check`` and ``ruff format --check`` would reject. Those failures are
        deterministic and cost a whole review cycle each, so the coordinator fixes them
        the same way a developer's editor would. Lint errors that ruff cannot fix and
        every other check still go to review unchanged.
        """
        targets = [
            path
            for path in changed
            if not self._generated_docs_path(path) and (worktree / path).is_file()
        ]
        python = [
            path.removeprefix("caliber/")
            for path in targets
            if path.startswith("caliber/") and path.endswith(".py")
        ]
        steps: list[tuple[str, list[str], Path]] = []
        interpreter = worktree / "caliber/.venv/bin/python"
        if python and interpreter.exists():
            ruff = [str(interpreter), "-m", "ruff"]
            caliber = worktree / "caliber"
            steps.append(("ruff-fix", [*ruff, "check", "--fix", "--quiet", *python], caliber))
            steps.append(("ruff-format", [*ruff, "format", "--quiet", *python], caliber))
        if targets:
            runner = str(interpreter) if interpreter.exists() else "python3"
            script = [runner, "-I", "-c", _WHITESPACE_SCRIPT, *targets]
            steps.append(("whitespace", script, worktree))
        results = []
        for label, args, cwd in steps:
            try:
                proc = subprocess.run(
                    self._as_worker(args),
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=300,
                    env=untrusted_env(),
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                results.append({"step": label, "error": str(error)[:300]})
                continue
            results.append(
                {
                    "step": label,
                    "exit_code": proc.returncode,
                    "output": (proc.stdout + proc.stderr)[-500:],
                }
            )
        if results:
            store.event(run, issue, "normalize", {"steps": results})

    def _sync_generated_docs(self, worktree: Path, store: Store, run: str, issue: int) -> None:
        """Generate canonical served docs before the candidate is reviewed."""
        args = ["node", "caliber/caliber-ui/scripts/sync-docs.mjs"]
        env = untrusted_env()
        env["CALIBER_DOCS_PYTHON"] = str(worktree / "caliber/.venv/bin/python")
        env["CALIBER_DOCS_STRICT"] = "1"
        proc = subprocess.run(
            self._as_worker(args),
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
            env=env,
        )
        store.event(
            run,
            issue,
            "docs_sync",
            {"exit_code": proc.returncode, "output": (proc.stdout + proc.stderr)[-1000:]},
        )
        if proc.returncode:
            raise RuntimeError(f"Documentation sync failed (exit {proc.returncode})")

    def _checks(self, issue: int, worktree: Path, changed: list[str]) -> list[dict]:
        issue_commands = self.settings.issue_checks.get(issue, ())
        unsupported = [path for path in changed if not self._standard_check_path(path)]
        results: list[dict] = []
        if unsupported and not issue_commands:
            # Reported with every other gate so one fix attempt sees all failures.
            results.append(
                {
                    "command": "affected-component checks",
                    "passed": False,
                    "output": "Issue-specific QA commands are required for: "
                    + ", ".join(unsupported),
                }
            )
        commands = [*self.settings.base_checks, *issue_commands]
        if any(self._generated_docs_path(path) for path in changed):
            ui_check = "scripts/ci-local.sh ui"
            if ui_check not in commands:
                commands.append(ui_check)
        changed_python = any(
            path.endswith(".py")
            and "tests" not in Path(path).parts
            and not Path(path).name.startswith("test_")
            and (worktree / path).is_file()
            for path in changed
        )
        changed_backend_tests = any(
            path.startswith("caliber/tests/") and path.endswith(".py") for path in changed
        )
        if (
            changed_backend_tests
            and not changed_python
            and "scripts/ci-local.sh test" not in commands
        ):
            commands.append("scripts/ci-local.sh test")
        automatic_coverage = None
        uncovered_modules: list[str] = []
        threshold = self.settings.coverage_threshold
        if changed_python and not any(
            self._coverage_command(item, threshold) for item in issue_commands
        ):
            tests, uncovered_modules = self._coverage_tests(worktree, changed)
            automatic_coverage = shlex.join(
                [
                    "caliber/.venv/bin/python",
                    "-m",
                    "pytest",
                    *(tests if tests is not None else ["caliber/tests"]),
                    "-n",
                    "4",
                    "--dist",
                    "loadgroup",
                    # Measure only the package, and print no terminal table: the
                    # coordinator reads the JSON and reports the exact shortfall.
                    "--cov=caliber/src/caliber",
                    "--cov-branch",
                    "--cov-report=",
                    "-q",
                ]
            )
            if not uncovered_modules:
                commands.append(automatic_coverage)
        for item in commands:
            args = shlex.split(item)
            if not args or any(token in {";", "&&", "|", ">"} for token in args):
                results.append({"command": item, "passed": False, "output": "Invalid command"})
                break
            try:
                with tempfile.TemporaryDirectory(prefix="autocd-coverage-") as directory:
                    if self.settings.worker_user:
                        os.chmod(directory, 0o733)
                    coverage_path = Path(directory) / "coverage.json"
                    coverage_check = (
                        self._coverage_command(item, threshold) or item == automatic_coverage
                    )
                    invocation = (
                        [*args, f"--cov-report=json:{coverage_path}"] if coverage_check else args
                    )
                    proc = subprocess.run(
                        self._as_worker(invocation),
                        cwd=worktree,
                        text=True,
                        stdout=subprocess.PIPE,
                        # One chronological stream: with stderr appended after stdout,
                        # the kept tail was stderr noise and lost ci-local's summary.
                        stderr=subprocess.STDOUT,
                        check=False,
                        timeout=self.settings.qa_command_timeout_seconds,
                        env=untrusted_env(),
                    )
                    coverage = (
                        self._coverage_evidence(coverage_path, worktree, changed, threshold)
                        if coverage_check
                        else None
                    )
                full = _ANSI.sub("", (proc.stdout or "") + (proc.stderr or ""))
                results.append(
                    {
                        "command": item,
                        "passed": proc.returncode == 0,
                        "exit_code": proc.returncode,
                        "output": self._check_output(args, full),
                    }
                )
                if args[0].endswith("ci-local.sh") and re.search(r"^SKIP\s", full, re.MULTILINE):
                    results[-1]["passed"] = False
                    results[-1]["output"] += "\nRequired CI parity job was skipped"
                if coverage_check:
                    results[-1]["coverage"] = coverage["summary"]
                    if not coverage["passed"]:
                        results[-1]["passed"] = False
                        results[-1]["output"] += "\n" + coverage["reason"]
            except subprocess.TimeoutExpired:
                results.append(
                    {
                        "command": item,
                        "passed": False,
                        "output": (
                            "QA command timed out after "
                            f"{self.settings.qa_command_timeout_seconds} seconds"
                        ),
                    }
                )
            except OSError as error:
                results.append({"command": item, "passed": False, "output": str(error)[:500]})
            # Every gate runs even after one fails: the developer gets one turn per
            # coordinator check round, so stopping early hid the next failure for a
            # whole cycle.
        if uncovered_modules and all(row["passed"] for row in results):
            results.append(
                {
                    "command": "changed-module coverage",
                    "passed": False,
                    "output": "No backend test references the changed production module(s): "
                    + ", ".join(uncovered_modules)
                    + ". Add focused tests under caliber/tests that import them.",
                }
            )
        return results

    @staticmethod
    def _coverage_tests(worktree: Path, changed: list[str]) -> tuple[list[str] | None, list[str]]:
        """Select the backend tests that exercise the changed production modules.

        The full backend suite takes over two hours on a hosted runner, longer than one
        QA command may run, so measuring changed-file coverage with it made every
        backend task time out. Coverage from a subset can only be lower than from the
        full suite, so a pass here is still a true threshold pass. Returns ``(None, [])``
        when a changed file is outside ``caliber/src`` (keep the full suite), and, when no
        test references any changed module, those modules so the developer adds tests.
        """
        production = [
            path
            for path in changed
            if path.endswith(".py")
            and "tests" not in Path(path).parts
            and not Path(path).name.startswith("test_")
            and (worktree / path).is_file()
        ]
        if any(not path.startswith("caliber/src/caliber/") for path in production):
            return None, []
        selected = {
            path
            for path in changed
            if path.startswith("caliber/tests/")
            and Path(path).name.startswith("test_")
            and path.endswith(".py")
            and (worktree / path).is_file()
        }
        tests = sorted((worktree / "caliber/tests").rglob("test_*.py"))
        texts = {test: test.read_text(errors="replace") for test in tests}
        for path in production:
            parts = Path(path).relative_to("caliber/src").with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            dotted = ".".join(parts)
            parent, _, name = dotted.rpartition(".")
            reference = re.compile(
                rf"\b{re.escape(dotted)}\b"
                + (
                    rf"|from\s+{re.escape(parent)}\s+import\s+"
                    rf"(?:[^\n(]*\b{re.escape(name)}\b|\([^)]*\b{re.escape(name)}\b)"
                    if parent
                    else ""
                )
            )
            selected |= {
                str(test.relative_to(worktree))
                for test, text in texts.items()
                if reference.search(text)
            }
        # A module no selected test reaches still fails the per-file coverage gate below,
        # with its own measured percentage; only an empty selection needs a separate hint.
        return sorted(selected), [] if selected else production

    @staticmethod
    def _check_output(args: list[str], text: str) -> str:
        """Keep the part of a command's output that explains its result.

        ``ci-local.sh`` runs every requested job and prints a summary last, so a plain
        tail shows the last job's noise rather than the job that failed. For it, keep
        the summary plus the end of each failing job's own section.
        """
        if not args or not args[0].endswith("ci-local.sh"):
            return text[-3000:]
        sections: dict[str, str] = {}
        current = None
        lines: list[str] = []
        for line in text.splitlines():
            header = re.fullmatch(r"── (\S+) ─+", line.strip())
            if header:
                if current:
                    sections[current] = "\n".join(lines)
                current, lines = header.group(1), []
            elif current:
                lines.append(line)
        if current:
            sections[current] = "\n".join(lines)
        summary = sections.pop("summary", None)
        if summary is None:
            return text[-3000:]
        failed = re.findall(r"^FAIL\s+(\S+)", summary, re.MULTILINE)
        parts = [f"── summary\n{summary.strip()[:1500]}"]
        for job in failed:
            if job in sections:
                parts.append(f"── {job} (end of output)\n{sections[job].strip()[-3000:]}")
        return "\n\n".join(parts)[-8000:]

    @staticmethod
    def _coverage_evidence(
        report: Path, worktree: Path, changed: list[str], threshold: float = 95.0
    ) -> dict:
        if not report.is_file():
            return {"passed": False, "summary": None, "reason": "Coverage JSON missing"}
        try:
            data = json.loads(report.read_text())
            # coverage.py's JSON report keeps per-file counts under "summary"; only
            # "totals" is flat. Reading the file rows flat raised KeyError for every
            # real report, so each changed module failed as "Invalid coverage JSON".
            raw = {(worktree / path).resolve(): entry for path, entry in data["files"].items()}
            files = {
                path: entry.get("summary", entry) if isinstance(entry, dict) else entry
                for path, entry in raw.items()
            }
            affected = [
                (worktree / path).resolve()
                for path in changed
                if path.endswith(".py")
                and "tests" not in Path(path).parts
                and not Path(path).name.startswith("test_")
                # CI's coverage config omits migrations; so does this gate.
                and not path.startswith("caliber/src/caliber/db/migrations/")
                and (worktree / path).is_file()
            ]
            measured = {path: files[path] for path in affected}
            totals = data["totals"]
            coverage_rows = {str(path.relative_to(worktree)): row for path, row in measured.items()}
            shortfalls = []
            for path, row in measured.items():
                denominator = row["num_statements"] + row.get("num_branches", 0)
                numerator = row["covered_lines"] + row.get("covered_branches", 0)
                if not denominator or 100 * numerator / denominator < threshold:
                    entry = raw[path] if isinstance(raw[path], dict) else {}
                    lines = entry.get("missing_lines") or []
                    branches = entry.get("missing_branches") or []
                    percent = 100 * numerator / denominator if denominator else 0.0
                    # Say by how much and where, so a developer without a shell knows
                    # which lines and branches its tests still need to reach.
                    shortfalls.append(
                        f"Coverage for {path.relative_to(worktree)} is {percent:.2f}%, below "
                        f"the {threshold:g}% threshold; missing lines {lines[:60]}; "
                        f"missing branches {branches[:30]}"
                    )
            if shortfalls:
                return {"passed": False, "summary": None, "reason": "\n".join(shortfalls)}
            return {
                "passed": True,
                "summary": ", ".join(
                    f"{name}={100 * (row['covered_lines'] + row.get('covered_branches', 0)) / (row['num_statements'] + row.get('num_branches', 0)):.2f}%"
                    for name, row in {"TOTAL": totals, **coverage_rows}.items()
                ),
                "reason": "",
            }
        except (KeyError, ValueError, TypeError, ZeroDivisionError, OSError) as error:
            return {
                "passed": False,
                "summary": None,
                "reason": f"Invalid coverage JSON: {type(error).__name__}",
            }

    def _setup(self, worktree: Path, store: Store, run: str, issue: int) -> None:
        for item in self.settings.setup_commands:
            args = shlex.split(item)
            if not args or any(token in {";", "&&", "|", ">"} for token in args):
                raise RuntimeError(f"Invalid setup command: {item}")
            proc = subprocess.run(
                self._as_worker(args),
                cwd=worktree,
                text=True,
                capture_output=True,
                check=False,
                timeout=3600,
                env=untrusted_env(),
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

    def _as_worker(self, args: list[str]) -> list[str]:
        if not self.settings.worker_user:
            return args
        return worker_args(self.settings.worker_user, args)

    @staticmethod
    def _coverage_command(item: str, threshold: float = 95.0) -> bool:
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
                return float(value) >= threshold
            except ValueError:
                return False
        return False

    @staticmethod
    def _blocking_findings(findings: object) -> list:
        """Return the findings that must be fixed before the candidate can be published.

        Only an explicit P3 improvement the reviewer marked non-blocking is let through;
        it stays in the PR body for the owner. Anything else, including a finding in an
        older or malformed shape, blocks.
        """
        if not isinstance(findings, list):
            return [findings]
        return [
            item
            for item in findings
            if not (
                isinstance(item, dict)
                and item.get("severity") == "P3"
                and item.get("blocking") is False
            )
        ]

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

Implements #{task.issue}: {task.title}. The live Project issue supplies the complete acceptance contract. The coordinator may merge this PR after all required gates pass.

Goal and motivation: {task.title}

Project target: {task.target}.

Implementation: {developer.get("summary", "See changed files below.")}

Changed files:

{files}

Design decisions: {developer.get("decisions", "Followed the issue contract and repository policy.")}

Candidate: `{head}`. Closes #{task.issue} after a guarded merge.

## Tests and validation

- [x] Added or updated tests for changed behavior; QA evaluated the issue-specific evidence.
- [x] Ran configured CI-equivalent checks for affected areas; see exact commands below.
- [x] Ran `git diff --check`.
- [x] Checked for secrets, unrelated changes, generated docs/assets, migrations, and deployment implications in review.

Validation commands and results:

{tests}

Review cycle {cycle}: {json.dumps(review, sort_keys=True)}

QA verdict: {json.dumps(qa, sort_keys=True)}

Developer remaining work at handoff: {developer.get("remaining_work", "Unspecified")}

## Risks and follow-up

Risks and limitations: {developer.get("risks", "Owner review and exact-head required CI remain pending when this PR opens.")}

Compatibility: {developer.get("compatibility", "See issue contract.")}

Migrations: {developer.get("migrations", "See issue contract.")}

Documentation: {developer.get("documentation", "See changed files.")}

Deployment: {developer.get("deployment", "No deployment performed by this workflow.")}

The coordinator verifies the exact head, branch protection, merge commit on main, issue closure and Project Done state before recording completion.

## Copilot handoff

- [ ] This PR is ready for review only after all applicable validation passes; merge requires the gates in `AGENTS.md`.
- [x] Reviewer status is reported accurately; model self-review is not independent GitHub approval.
"""

    def _post_pr_bookkeeping(self, store: Store, run: str, issue: int, pr: int) -> bool:
        """Move the Project item to In Review and record reviewers, once, retrying later.

        Returns True when both writes are recorded. A failure is logged and left for
        the next ``_check_pr`` tick; it never blocks a published PR.
        """
        done = {
            json.loads(row["detail"]).get("step")
            for row in store.db.execute(
                "SELECT detail FROM events WHERE run_id=? AND issue=? AND kind='post_pr_step'",
                (run, issue),
            )
        }
        if {"review", "reviewers"} <= done:
            return True
        task = store.task(run, issue)
        evidence_row = store.db.execute(
            "SELECT detail FROM events WHERE run_id=? AND issue=? AND kind='publish_evidence' "
            "ORDER BY id DESC LIMIT 1",
            (run, issue),
        ).fetchone()
        evidence = (
            json.loads(evidence_row["detail"])["evidence"]
            if evidence_row
            else f"PR #{pr} at {task['head']}; QA commands passed"
        )
        try:
            with self.mutation_lock:
                if "review" not in done:
                    self.github.transition(
                        task["target"], issue, "review", {"evidence": [evidence]}
                    )
                    store.event(run, issue, "post_pr_step", {"step": "review"})
                if "reviewers" not in done:
                    owner = (
                        None if self.settings.auto_merge else self.settings.repository.split("/")[0]
                    )
                    reviewers = self.github.request_reviewers(self.settings.root, pr, owner)
                    self.github.record_reviewers(pr, reviewers)
                    store.event(run, issue, "reviewer_requests", reviewers)
                    store.event(run, issue, "post_pr_step", {"step": "reviewers"})
        except Exception as error:  # noqa: BLE001 - retried on the next PR check
            store.event(run, issue, "post_pr_error", str(error)[:500])
            return False
        return True

    def _check_pr(self, store: Store, run: str, issue: int, pr: int) -> dict:
        try:
            state = self.github.pr_state(pr)
            task = store.task(run, issue)
            expected = task["head"]
            if state.get("state") == "MERGED":
                # The owner may have pushed a fix to the branch before merging; a
                # verified merge on main completes the task whatever the head is.
                return self._reconcile_merged(store, run, issue, pr, state)
            self._post_pr_bookkeeping(store, run, issue, pr)
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
            if state.get("state") != "OPEN":
                store.phase(run, issue, "blocked", failure="PR closed without a verified merge")
                return {"issue": issue, "status": "blocked_closed_pr", "pr": state["url"]}
            if (
                not self.settings.auto_merge
                and state.get("isDraft") is False
                and store.task(run, issue)["phase"] == "ready"
            ):
                return {"issue": issue, "status": "ready", "pr": state["url"]}
            owner = self.settings.repository.split("/")[0]
            author = state.get("author", {}).get("login", "")
            checks = self.github.required_checks(pr, state)
            unresolved = self.github.unresolved_threads(pr)
            # Without automatic merge the owner is the merge gate and reads every thread
            # (Copilot reviews drafts automatically); an unresolved thread must not keep
            # the PR in draft and stall the pipeline. Automatic merge still requires zero.
            ready, reasons = pr_ready(
                state,
                checks,
                unresolved if self.settings.auto_merge else 0,
                owner,
                author,
                require_owner=not self.settings.auto_merge,
            )
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
                    if not self.settings.auto_merge:
                        store.phase(run, issue, "ready")
                        return {"issue": issue, "status": "ready_for_owner", "pr": state["url"]}
                    return {"issue": issue, "status": "waiting_for_merge", "pr": state["url"]}
                if not self.settings.auto_merge:
                    store.phase(run, issue, "ready")
                    return {"issue": issue, "status": "ready_for_owner", "pr": state["url"]}
                with self.mutation_lock:
                    fresh = self.github.pr_state(pr)
                    if (
                        fresh.get("state") != "OPEN"
                        or fresh["headRefOid"] != expected
                        or fresh["baseRefName"] != self.settings.base_branch
                        or fresh["headRefName"] != task["branch"]
                    ):
                        return {
                            "issue": issue,
                            "status": "waiting_for_revalidation",
                            "pr": state["url"],
                        }
                    fresh_checks = self.github.required_checks(pr, fresh)
                    fresh_unresolved = self.github.unresolved_threads(pr)
                    merge_ready, merge_reasons = pr_ready(
                        fresh,
                        fresh_checks,
                        fresh_unresolved,
                        owner,
                        fresh.get("author", {}).get("login", ""),
                        require_owner=False,
                    )
                    if not merge_ready or fresh.get("isDraft"):
                        return {
                            "issue": issue,
                            "status": "waiting_for_revalidation",
                            "pr": state["url"],
                            "reasons": merge_reasons,
                        }
                    if not self.github.branch_up_to_date(expected, task["branch"]):
                        reason = "Base branch advanced; same-branch rebase and full review/QA are required"
                        store.event(
                            run,
                            issue,
                            "base_moved",
                            {"head": expected, "base": self.settings.base_branch},
                        )
                        store.phase(run, issue, "blocked", failure=reason)
                        try:
                            self.github.transition(
                                task["target"], issue, "blocked", {"reason": reason}
                            )
                        except Exception as error:  # noqa: BLE001 - local block remains durable
                            store.event(run, issue, "remote_block_error", str(error)[:500])
                        return {
                            "issue": issue,
                            "status": "blocked_base_moved",
                            "pr": state["url"],
                            "reasons": [reason],
                        }
                    self.github.merge_pr(pr, expected)
                merged = self.github.pr_state(pr)
                if merged.get("state") == "MERGED":
                    return self._reconcile_merged(store, run, issue, pr, merged)
                return {"issue": issue, "status": "waiting_for_merge_queue", "pr": state["url"]}
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
            # An error that never clears must not hold the PR in ci_wait forever:
            # ci_wait counts as active work and would stop every later task.
            phase = store.task(run, issue)["phase"]
            started = store.phase_started(run, issue, phase)
            if (
                phase in {"ci_wait", "merged_unreconciled"}
                and started
                and (datetime.now(UTC) - started).total_seconds() > self.settings.ci_timeout_seconds
            ):
                reason = (
                    f"PR #{pr} could not be checked for {self.settings.ci_timeout_seconds} seconds"
                )
                store.phase(run, issue, "blocked", failure=reason)
                return {"issue": issue, "status": "blocked_pr_check_error"}
            return {"issue": issue, "status": "pr_check_error", "error": str(error)[:500]}

    def _reconcile_merged(self, store: Store, run: str, issue: int, pr: int, state: dict) -> dict:
        task = store.task(run, issue)
        store.phase(run, issue, "merged_unreconciled")
        merge_oid = (state.get("mergeCommit") or {}).get("oid", "")
        self.github.verify_merged_on_main(merge_oid)
        snapshot = self.github.snapshot()
        item = next(
            (
                row
                for row in snapshot["items"]
                if row.get("issue") and row["issue"]["number"] == issue
            ),
            None,
        )
        if not item:
            raise RuntimeError("Merged issue is missing from Project")
        if item["issue"]["state"] == "OPEN":
            self.github.close_issue(issue)
            snapshot = self.github.snapshot()
            item = next(
                (
                    row
                    for row in snapshot["items"]
                    if row.get("issue") and row["issue"]["number"] == issue
                ),
                None,
            )
        if (
            not item
            or item["issue"]["state"] != "CLOSED"
            or item["issue"].get("stateReason") != "COMPLETED"
        ):
            raise RuntimeError("Merged issue has not closed as COMPLETED")
        if item["fields"].get("Status") != "Done" or item["fields"].get("Readiness") != "Done":
            self.github.transition(
                task["target"],
                issue,
                "done",
                {
                    "evidence": [
                        f"PR #{pr} merged at {state.get('mergedAt')}; main commit {merge_oid}; required checks passed at head {task['head']}"
                    ]
                },
            )
            snapshot = self.github.snapshot()
            item = next(
                (
                    row
                    for row in snapshot["items"]
                    if row.get("issue") and row["issue"]["number"] == issue
                ),
                None,
            )
        if (
            not item
            or item["fields"].get("Status") != "Done"
            or item["fields"].get("Readiness") != "Done"
        ):
            raise RuntimeError("Project Done transition did not persist")
        store.event(run, issue, "merged_done", {"pr": pr, "merge_commit": merge_oid})
        store.phase(run, issue, "done")
        return {
            "issue": issue,
            "status": "merged_done",
            "pr": state["url"],
            "merge_commit": merge_oid,
        }
