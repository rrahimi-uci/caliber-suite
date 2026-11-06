"""GitHub and Git operations; arguments never pass through a shell."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .config import Settings


class CommandError(RuntimeError):
    pass


def command(args: list[str], cwd: Path, *, timeout: int = 120, check: bool = True) -> str:
    result = subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False
    )
    if check and result.returncode:
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
            ["node", "automated-cd/scripts/project_snapshot.mjs"], self.root, timeout=180
        )
        snapshot = json.loads(data)
        if snapshot["repository"]["nameWithOwner"] != self.settings.repository:
            raise CommandError("Configured repository does not match the authoritative Project")
        if snapshot["repository"]["defaultBranch"] != self.settings.base_branch:
            raise CommandError("Configured base branch does not match the repository default")
        return snapshot

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
                ],
                worktree,
                timeout=180,
            )
        match = re.search(r"/pull/(\d+)", output)
        if not match:
            raise CommandError("Could not identify created PR")
        return int(match.group(1))

    def request_reviewers(self, worktree: Path, pr: int, owner: str) -> dict[str, str]:
        status = {}
        for label, reviewer in (
            ("owner", owner),
            ("copilot", "@copilot"),
        ):
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
                    "number,url,headRefOid,headRefName,baseRefName,mergeable,isDraft,reviewRequests,reviewDecision,statusCheckRollup,latestReviews,author,files",
                ],
                self.root,
            )
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
            check=False,
        )
        branch_required = json.loads(output) if output.strip() else []
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
    state: dict, checks: list[dict], unresolved: int, owner: str, author: str
) -> tuple[bool, list[str]]:
    reasons = []
    if state["mergeable"] != "MERGEABLE":
        reasons.append("PR is not mergeable")
    if not checks or any(check["state"] not in {"SUCCESS", "PASS"} for check in checks):
        reasons.append("required CI checks are missing, pending, skipped, or failing")
    if unresolved:
        reasons.append(f"{unresolved} unresolved review threads")
    requested = {entry.get("login") for entry in state.get("reviewRequests", [])}
    reviewed_by_owner = any(
        review.get("author", {}).get("login") == owner for review in state.get("latestReviews", [])
    )
    if owner != author and owner not in requested and not reviewed_by_owner:
        reasons.append("owner review is not requested")
    if any(review.get("state") == "CHANGES_REQUESTED" for review in state.get("latestReviews", [])):
        reasons.append("changes requested")
    return not reasons, reasons
