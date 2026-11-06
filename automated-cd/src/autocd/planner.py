"""Deterministic eligibility and nonconflicting task assignment."""

from __future__ import annotations

from dataclasses import dataclass


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


def eligible(
    snapshot: dict,
    *,
    allowlist: tuple[int, ...] = (),
    pathmap: dict[int, frozenset[str]] | None = None,
) -> list[Assignment]:
    repository = snapshot["repository"]["nameWithOwner"]
    result = []
    for item in snapshot["items"]:
        issue = item.get("issue")
        if not issue or item.get("isArchived") or issue["repository"] != repository:
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
        if len(selected) >= min(workers, limit):
            break
        if not any(_conflict(task.paths, other.paths) for other in selected):
            selected.append(task)
    return selected
