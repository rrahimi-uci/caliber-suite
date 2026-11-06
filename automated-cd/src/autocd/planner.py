"""Deterministic eligibility and nonconflicting task assignment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Assignment:
    issue: int
    title: str
    body: str
    target: str
    paths: frozenset[str]
    priority: int


def _target(item: dict) -> str | None:
    fields, issue = item["fields"], item["issue"]
    if fields.get("Release"):
        return "release:" + fields["Release"]
    if issue.get("milestone", {}).get("number"):
        return "milestone:#" + str(issue["milestone"]["number"])
    return None


def view_label(snapshot: dict, number: int) -> str:
    view = snapshot.get("view") or {}
    if view.get("number") != number:
        raise ValueError("Configured Project view does not match the live snapshot")
    parts = (view.get("filter") or "").split()
    labels = [part.removeprefix("label:") for part in parts if part.startswith("label:")]
    if (
        len(parts) != 2
        or "is:open" not in parts
        or len(labels) != 1
        or not labels[0].startswith("release:")
    ):
        raise ValueError(
            "Project view filter changed; only is:open plus one release label is supported"
        )
    return labels[0]


def completed_today(
    snapshot: dict, closed_issues: list[dict], label: str, day: str, timezone: str
) -> list[int]:
    closed = {row["number"]: row for row in closed_issues}
    done = []
    for item in snapshot["items"]:
        issue = item.get("issue")
        if (
            not issue
            or item.get("isArchived")
            or issue.get("repository") != snapshot["repository"]["nameWithOwner"]
        ):
            continue
        fields = item["fields"]
        if (
            label not in issue.get("labels", [])
            or fields.get("Item type") != "Task"
            or fields.get("Status") != "Done"
            or fields.get("Readiness") != "Done"
            or issue.get("state") != "CLOSED"
            or issue.get("stateReason") != "COMPLETED"
        ):
            continue
        row = closed.get(issue["number"])
        if not row or row.get("stateReason") != "COMPLETED" or not row.get("closedAt"):
            continue
        merged_at = [
            datetime.fromisoformat(pr["mergedAt"])
            for pr in issue.get("prs", [])
            if pr.get("state") == "MERGED" and pr.get("mergedAt")
        ]
        if not merged_at:
            continue
        completed_at = max(datetime.fromisoformat(row["closedAt"]), *merged_at)
        if completed_at.astimezone(ZoneInfo(timezone)).date().isoformat() != day:
            continue
        done.append(issue["number"])
    return sorted(set(done))


def eligible(
    snapshot: dict,
    *,
    allowlist: tuple[int, ...] = (),
    pathmap: dict[int, frozenset[str]] | None = None,
    scope_label: str | None = None,
) -> list[Assignment]:
    repository = snapshot["repository"]["nameWithOwner"]
    result = []
    for item in snapshot["items"]:
        issue = item.get("issue")
        if not issue or item.get("isArchived") or issue["repository"] != repository:
            continue
        if scope_label and scope_label not in issue.get("labels", []):
            continue
        fields = item["fields"]
        if issue["state"] != "OPEN" or fields.get("Status") != "Todo":
            continue
        if fields.get("Readiness") != "Ready" or fields.get("Item type") != "Task":
            continue
        if allowlist and issue["number"] not in allowlist:
            continue
        if issue.get("assignees") or any(pr["state"] == "OPEN" for pr in issue.get("prs", [])):
            continue
        human = fields.get("Human input") or "Not assessed"
        if human != "None pending" and not all(
            part.strip().endswith(": acceptance") for part in human.split(",")
        ):
            continue
        if any(
            dep["state"] != "CLOSED" or dep.get("stateReason") != "COMPLETED"
            for dep in issue.get("blockedBy", [])
        ):
            continue
        target = _target(item)
        if not target:
            continue
        priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(fields.get("Priority"), 4)
        # Only coordinator-reviewed explicit ownership can permit parallel edits.
        paths = (pathmap or {}).get(issue["number"], frozenset())
        result.append(
            Assignment(issue["number"], issue["title"], issue["body"], target, paths, priority)
        )
    return sorted(result, key=lambda task: (task.priority, task.issue))


def _conflict(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return True
    return any(
        x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/")
        for x in a
        for y in b
    )


def assign(candidates: list[Assignment], workers: int, limit: int) -> list[Assignment]:
    selected: list[Assignment] = []
    for task in candidates:
        if len(selected) >= limit:
            break
        if not any(_conflict(task.paths, other.paths) for other in selected):
            selected.append(task)
    return selected
