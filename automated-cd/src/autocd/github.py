"""GitHub and Git operations; arguments never pass through a shell."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .config import Settings


class CommandError(RuntimeError):
    pass


_AUDIT: ContextVar[Callable[[str, dict], None] | None] = ContextVar(
    "autocd_command_audit", default=None
)


@contextmanager
def audit_commands(callback: Callable[[str, dict], None]) -> Iterator[None]:
    token = _AUDIT.set(callback)
    try:
        yield
    finally:
        _AUDIT.reset(token)


def command(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
    check: bool = True,
    allowed_exit_codes: tuple[int, ...] = (0,),
) -> str:
    audit = _AUDIT.get()
    if audit:
        audit("command_started", {"argv": args, "cwd": str(cwd)})
    try:
        result = subprocess.run(
            args, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        if audit:
            audit("command_failed", {"argv": args, "error": type(error).__name__})
        raise
    if audit:
        audit("command_completed", {"argv": args, "exit_code": result.returncode})
    if check and result.returncode not in allowed_exit_codes:
        # External output can contain private data; keep failure diagnostics bounded.
        raise CommandError(
            f"{' '.join(args[:3])} exited {result.returncode}: {result.stderr[-500:]}"
        )
    return result.stdout


class GitHub:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.root

    def snapshot(self) -> dict:
        data = command(
            [
                "node",
                "automated-cd/scripts/project_snapshot.mjs",
                str(self.settings.project_view_number),
            ],
            self.root,
            timeout=180,
        )
        snapshot = json.loads(data)
        if snapshot["repository"]["nameWithOwner"] != self.settings.repository:
            raise CommandError("Configured repository does not match the authoritative Project")
        if snapshot["repository"]["defaultBranch"] != self.settings.base_branch:
            raise CommandError("Configured base branch does not match the repository default")
        if snapshot.get("view", {}).get("number") != self.settings.project_view_number:
            raise CommandError("Configured Project view is unavailable or changed")
        return snapshot

    def closed_issues_since(self, since: str, label: str) -> list[dict]:
        rows = json.loads(
            command(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    self.settings.repository,
                    "--state",
                    "closed",
                    "--search",
                    f"label:{label} closed:>={since}",
                    "--limit",
                    "1000",
                    "--json",
                    "number,closedAt,stateReason",
                ],
                self.root,
            )
        )
        if len(rows) >= 1000:
            raise CommandError(
                "Daily closure search reached the CLI limit; paginate before counting"
            )
        return rows

    def overview(self) -> dict:
        repo = self.settings.repository

        def query(args: list[str]) -> object:
            return json.loads(command(["gh", *args], self.root))

        open_prs = query(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,headRefName,reviewDecision,mergeable,statusCheckRollup,comments,latestReviews",
            ]
        )
        if len(open_prs) >= 1000:
            raise CommandError("Open PR inventory reached the CLI limit; paginate before planning")
        return {
            "open_prs": open_prs,
            "recent_runs": query(
                [
                    "run",
                    "list",
                    "--repo",
                    repo,
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,status,conclusion,headSha",
                ]
            ),
            "recent_commits": query(["api", f"repos/{repo}/commits?per_page=20"]),
            "branches": query(
                ["api", "--paginate", "--slurp", f"repos/{repo}/branches?per_page=100"]
            ),
        }

    def login(self) -> str:
        return command(["gh", "api", "user", "--jq", ".login"], self.root).strip()

    def transition(self, target: str, issue: int, phase: str, payload: dict) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=True) as file:
            json.dump(payload, file)
            file.flush()
            args = [
                "node",
                ".agents/bin/engineering.mjs",
                "transition",
                target,
                str(issue),
                phase,
                "--file",
                file.name,
            ]
            command(args, self.root, timeout=180)  # mandatory dry run
            command([*args, "--apply"], self.root, timeout=180)

    def prepare_worktree(self, issue: int, run: str) -> tuple[str, Path]:
        branch = f"autocd/issue-{issue}-{run[:8]}"
        destination = self.settings.state_dir / "worktrees" / branch.replace("/", "-")
        if destination.exists():
            raise CommandError(f"Worktree already exists: {destination}")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        command(["git", "fetch", "origin", self.settings.base_branch], self.root, timeout=180)
        command(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(destination),
                f"origin/{self.settings.base_branch}",
            ],
            self.root,
            timeout=180,
        )
        return branch, destination

    def changed(self, worktree: Path) -> list[str]:
        output = command(["git", "status", "--porcelain", "--untracked-files=all"], worktree)
        return [line[3:] for line in output.splitlines() if line]

    def commit_and_push(self, worktree: Path, branch: str, issue: int) -> str:
        if not self.changed(worktree):
            raise CommandError("Developer produced no changes")
        command(["git", "diff", "--check"], worktree)
        command(["git", "add", "--all"], worktree)
        command(["git", "diff", "--cached", "--check"], worktree)
        command(["git", "commit", "-m", f"feat(autocd): implement issue #{issue}"], worktree)
        head = command(["git", "rev-parse", "HEAD"], worktree).strip()
        command(["git", "push", "-u", "origin", branch], worktree, timeout=180)
        return head

    def create_pr(self, worktree: Path, title: str, body: str) -> int:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=True) as file:
            file.write(body)
            file.flush()
            output = command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    self.settings.repository,
                    "--base",
                    self.settings.base_branch,
                    "--title",
                    title,
                    "--body-file",
                    file.name,
                    "--draft",
                ],
                worktree,
                timeout=180,
            )
        match = re.search(r"/pull/(\d+)", output)
        if not match:
            raise CommandError("Could not identify created PR")
        return int(match.group(1))

    def request_reviewers(self, worktree: Path, pr: int, owner: str | None) -> dict[str, str]:
        status = {}
        reviewers = ([("owner", owner)] if owner else []) + [("copilot", "@copilot")]
        if not owner:
            status["owner"] = "not requested: automatic merge is enabled"
        for label, reviewer in reviewers:
            try:
                command(
                    [
                        "gh",
                        "pr",
                        "edit",
                        str(pr),
                        "--repo",
                        self.settings.repository,
                        "--add-reviewer",
                        reviewer,
                    ],
                    worktree,
                )
                status[label] = "requested"
            except CommandError as error:
                status[label] = f"unavailable: {error}"
        observed = self.pr_state(pr)
        requests = {entry.get("login", "") for entry in observed.get("reviewRequests", [])}
        reviews = {
            entry.get("author", {}).get("login", "") for entry in observed.get("latestReviews", [])
        }
        if owner and observed.get("author", {}).get("login") == owner:
            status["owner"] = "unavailable: GitHub does not allow author self-review requests"
        elif owner and owner in reviews:
            status["owner"] = "reviewed"
        elif owner and owner not in requests and status["owner"] == "requested":
            status["owner"] = "not confirmed by GitHub"
        if any("copilot" in login.lower() for login in reviews):
            status["copilot"] = "reviewed"
        elif (
            not any("copilot" in login.lower() for login in requests)
            and status["copilot"] == "requested"
        ):
            status["copilot"] = "not confirmed by GitHub"
        return status

    def record_reviewers(self, pr: int, status: dict[str, str]) -> None:
        current = json.loads(
            command(
                ["gh", "pr", "view", str(pr), "--repo", self.settings.repository, "--json", "body"],
                self.root,
            )
        )["body"]
        marker = "Reviewer requests: "
        if marker in current:
            raise CommandError("Reviewer status already recorded; inspect PR before retry")
        updated = (
            current
            + "\n"
            + marker
            + "; ".join(f"{name}={result}" for name, result in status.items())
            + "\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=True) as file:
            file.write(updated)
            file.flush()
            command(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr),
                    "--repo",
                    self.settings.repository,
                    "--body-file",
                    file.name,
                ],
                self.root,
            )

    def pr_state(self, pr: int) -> dict:
        return json.loads(
            command(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr),
                    "--repo",
                    self.settings.repository,
                    "--json",
                    "number,url,state,mergedAt,mergeCommit,headRefOid,headRefName,baseRefName,baseRefOid,mergeable,mergeStateStatus,isDraft,reviewRequests,reviewDecision,statusCheckRollup,latestReviews,author,files",
                ],
                self.root,
            )
        )

    def merge_pr(self, pr: int, head: str) -> None:
        command(
            [
                "gh",
                "pr",
                "merge",
                str(pr),
                "--repo",
                self.settings.repository,
                "--squash",
                "--match-head-commit",
                head,
            ],
            self.root,
            timeout=180,
        )

    def branch_up_to_date(self, head: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise CommandError("PR head is not a valid commit SHA")
        command(["git", "fetch", "origin", self.settings.base_branch], self.root, timeout=180)
        try:
            command(
                ["git", "merge-base", "--is-ancestor", f"origin/{self.settings.base_branch}", head],
                self.root,
            )
        except CommandError:
            return False
        return True

    def verify_merged_on_main(self, merge_oid: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", merge_oid):
            raise CommandError("Merged PR has no valid merge commit")
        command(["git", "fetch", "origin", self.settings.base_branch], self.root, timeout=180)
        command(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                merge_oid,
                f"origin/{self.settings.base_branch}",
            ],
            self.root,
        )

    def close_issue(self, issue: int) -> None:
        command(
            [
                "gh",
                "issue",
                "close",
                str(issue),
                "--repo",
                self.settings.repository,
                "--reason",
                "completed",
            ],
            self.root,
        )

    def mark_ready_body(self, pr: int, checks: list[dict]) -> None:
        current = json.loads(
            command(
                ["gh", "pr", "view", str(pr), "--repo", self.settings.repository, "--json", "body"],
                self.root,
            )
        )["body"]
        marker = "- [ ] This PR is ready for review only after all applicable validation passes; merge requires the gates in `AGENTS.md`."
        checked = marker.replace("[ ]", "[x]")
        if checked in current:
            return
        if marker not in current:
            raise CommandError("PR template readiness marker missing; inspect body before update")
        updated = current.replace(marker, checked, 1)
        updated += (
            "\nRequired checks at readiness: "
            + ", ".join(f"{check['name']}={check['state']}" for check in checks)
            + "\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=True) as file:
            file.write(updated)
            file.flush()
            command(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr),
                    "--repo",
                    self.settings.repository,
                    "--body-file",
                    file.name,
                ],
                self.root,
            )

    def required_checks(self, pr: int, state: dict) -> list[dict]:
        output = command(
            [
                "gh",
                "pr",
                "checks",
                str(pr),
                "--repo",
                self.settings.repository,
                "--required",
                "--json",
                "name,state",
            ],
            self.root,
            allowed_exit_codes=(0, 8),  # gh pr checks uses 8 while checks are pending.
        )
        if not output.strip():
            raise CommandError("Required-check inventory returned no JSON")
        branch_required = json.loads(output)
        rollup = {}
        for entry in state.get("statusCheckRollup", []):
            name = entry.get("name") or entry.get("context")
            if name:
                conclusion = entry.get("conclusion") or entry.get("state")
                check_state = (
                    "PENDING"
                    if entry.get("status") not in {None, "COMPLETED"}
                    else "PASS"
                    if conclusion in {"SUCCESS", "PASS"}
                    else "SKIPPING"
                    if conclusion in {"SKIPPED", "NEUTRAL"}
                    else "FAIL"
                    if conclusion in {"FAILURE", "ERROR", "CANCELLED"}
                    else "PENDING"
                )
                rollup[name] = {"name": name, "state": check_state}
        by_name = {row["name"]: row for row in branch_required}
        configured = list(self.settings.required_checks)
        if any(
            file.get("path", "").startswith("automated-cd/")
            or file.get("path") == ".github/workflows/automated-cd.yml"
            for file in state.get("files", [])
        ):
            configured.append("Automated CD tests")
        for name in configured:
            by_name[name] = rollup.get(name, {"name": name, "state": "MISSING"})
        return list(by_name.values())

    def unresolved_threads(self, pr: int) -> int:
        query = "query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){nodes{isResolved}pageInfo{hasNextPage endCursor}}}}}"
        owner, name = self.settings.repository.split("/")
        cursor = None
        count = 0
        while True:
            args = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={pr}",
            ]
            if cursor:
                args += ["-F", f"after={cursor}"]
            page = json.loads(command(args, self.root))["data"]["repository"]["pullRequest"][
                "reviewThreads"
            ]
            count += sum(not node["isResolved"] for node in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return count
            cursor = page["pageInfo"]["endCursor"]


def pr_ready(
    state: dict,
    checks: list[dict],
    unresolved: int,
    owner: str,
    author: str,
    *,
    require_owner: bool = True,
) -> tuple[bool, list[str]]:
    reasons = []
    if state["mergeable"] != "MERGEABLE":
        reasons.append("PR is not mergeable")
    if not state.get("isDraft") and state.get("mergeStateStatus") not in {None, "CLEAN"}:
        reasons.append("GitHub reports merge blocked, behind, or not yet computed")
    if state.get("reviewDecision") == "REVIEW_REQUIRED":
        reasons.append("branch protection requires an independent review")
    if not checks or any(check["state"] not in {"SUCCESS", "PASS"} for check in checks):
        reasons.append("required CI checks are missing, pending, skipped, or failing")
    if unresolved:
        reasons.append(f"{unresolved} unresolved review threads")
    requested = {entry.get("login") for entry in state.get("reviewRequests", [])}
    reviewed_by_owner = any(
        review.get("author", {}).get("login") == owner for review in state.get("latestReviews", [])
    )
    if require_owner and owner != author and owner not in requested and not reviewed_by_owner:
        reasons.append("owner review is not requested")
    if any(review.get("state") == "CHANGES_REQUESTED" for review in state.get("latestReviews", [])):
        reasons.append("changes requested")
    return not reasons, reasons
