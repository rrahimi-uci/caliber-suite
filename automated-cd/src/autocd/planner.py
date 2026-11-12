"""Deterministic eligibility and nonconflicting task assignment."""

from __future__ import annotations

import json
import re
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
    execution_order: int | None = None
    readiness: str = "Ready"


@dataclass(frozen=True)
class DecisionCandidate:
    """A start-gated task that can receive a read-only decision packet.

    This deliberately does not contain writable paths. A decision analyst may inspect
    the repository and prepare options, but it cannot claim, edit, transition, approve,
    publish, or merge the task. Native dependencies must still be complete so the
    packet describes a decision that could actually unblock the next governed step.
    """

    issue: int
    title: str
    body: str
    target: str
    priority: int
    execution_order: int | None
    readiness: str
    human_input: str
    dependencies: tuple[int, ...]


_ENGINEERING_META = re.compile(r"<!--\s*caliber-engineering-meta:(.*?)\s*-->", re.DOTALL)


def declared_owned_paths(item: dict) -> frozenset[str] | None:
    """Return validated issue ownership metadata, or None when it is unusable.

    The issue helper writes this marker when a task is created. Hosted execution
    must not infer a writable scope from prose: missing, duplicated, malformed or
    unsafe metadata is treated as unknown ownership.
    """
    body = (item.get("issue") or {}).get("body")
    if not isinstance(body, str):
        return None
    matches = _ENGINEERING_META.findall(body)
    if len(matches) != 1:
        return None
    try:
        metadata = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    paths = metadata.get("owned_paths") if isinstance(metadata, dict) else None
    if not isinstance(paths, list) or not paths:
        return None
    validated: set[str] = set()
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or path != path.strip()
            or path.startswith("/")
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            return None
        validated.add(path)
    return frozenset(validated)


def _target(item: dict) -> str | None:
    fields, issue = item["fields"], item["issue"]
    if fields.get("Release"):
        return "release:" + fields["Release"]
    if issue.get("milestone", {}).get("number"):
        return "milestone:#" + str(issue["milestone"]["number"])
    return None


def _execution_order(item: dict) -> int | None:
    """Read the numeric Project order, preserving a deterministic fallback for missing data."""
    value = item["fields"].get("Execution order")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def view_label(snapshot: dict, number: int) -> str | None:
    view = snapshot.get("view") or {}
    if view.get("number") != number:
        raise ValueError("Configured Project view does not match the live snapshot")
    if number == 0:
        if view.get("filter"):
            raise ValueError("All-Project scope unexpectedly has a view filter")
        return None
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
    snapshot: dict, closed_issues: list[dict], label: str | None, day: str, timezone: str
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
            (label is not None and label not in issue.get("labels", []))
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
    require_owned_paths: bool = False,
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
        if fields.get("Readiness") not in {"Planned", "Ready"} or fields.get("Item type") != "Task":
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
        priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(fields.get("Priority"))
        if priority is None:
            continue
        execution_order = _execution_order(item)
        # Configured paths are an explicit coordinator override. Otherwise use the
        # validated issue marker. Hosted execution enables require_owned_paths so a
        # broad audit cannot consume a daily model attempt without a bounded scope.
        configured_paths = (pathmap or {}).get(issue["number"], frozenset())
        declared_paths = declared_owned_paths(item)
        if configured_paths:
            paths = configured_paths
        elif declared_paths is not None:
            paths = declared_paths
        elif require_owned_paths:
            continue
        else:
            paths = frozenset()
        result.append(
            Assignment(
                issue["number"],
                issue["title"],
                issue["body"],
                target,
                paths,
                priority,
                execution_order,
                fields["Readiness"],
            )
        )
    return sorted(
        result,
        key=lambda task: (
            task.execution_order is None,
            task.execution_order if task.execution_order is not None else 0,
            task.priority,
            task.issue,
        ),
    )


def decision_candidates(
    snapshot: dict,
    *,
    scope_label: str | None = None,
) -> list[DecisionCandidate]:
    """Find dependency-clear tasks whose only start blocker is an explicit decision.

    The scheduler must not silently convert a model recommendation into an approval.
    These candidates are therefore kept separate from :func:`eligible`: they receive
    a bounded, read-only proposal and remain in their governed Project state until the
    owner records the required decision.
    """
    repository = snapshot["repository"]["nameWithOwner"]
    result: list[DecisionCandidate] = []
    for item in snapshot["items"]:
        issue = item.get("issue")
        if not issue or item.get("isArchived") or issue.get("repository") != repository:
            continue
        if scope_label and scope_label not in issue.get("labels", []):
            continue
        fields = item["fields"]
        if issue.get("state") != "OPEN":
            continue
        if fields.get("Status") != "Todo" or fields.get("Item type") != "Task":
            continue
        if fields.get("Readiness") not in {"Planned", "Ready", "Needs input"}:
            continue
        if issue.get("assignees") or any(pr.get("state") == "OPEN" for pr in issue.get("prs", [])):
            continue
        human = fields.get("Human input") or "Not assessed"
        gates = [part.strip() for part in human.split(",")]
        if human == "None pending" or not any(gate.endswith(": start") for gate in gates):
            continue
        dependencies = tuple(
            sorted(
                int(dep["number"])
                for dep in issue.get("blockedBy", [])
                if type(dep.get("number")) is int
            )
        )
        if any(
            dep.get("state") != "CLOSED" or dep.get("stateReason") != "COMPLETED"
            for dep in issue.get("blockedBy", [])
        ):
            continue
        target = _target(item)
        if not target:
            continue
        priority = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(fields.get("Priority"))
        if priority is None:
            continue
        result.append(
            DecisionCandidate(
                issue=issue["number"],
                title=issue["title"],
                body=issue.get("body") or "",
                target=target,
                priority=priority,
                execution_order=_execution_order(item),
                readiness=fields["Readiness"],
                human_input=human,
                dependencies=dependencies,
            )
        )
    return sorted(
        result,
        key=lambda task: (
            task.execution_order is None,
            task.execution_order if task.execution_order is not None else 0,
            task.priority,
            task.issue,
        ),
    )


def _conflict(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return True
    return any(
        x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/")
        for x in a
        for y in b
    )


def assign(candidates: list[Assignment], workers: int, limit: int) -> list[Assignment]:
    if not candidates or limit <= 0:
        return []
    ordered = [task for task in candidates if task.execution_order is not None]
    if ordered:
        earliest = min(task.execution_order for task in ordered)
        candidates = [task for task in candidates if task.execution_order == earliest]
    # `eligible()` already orders candidates by priority and issue within an order
    # bucket. Preserve that order here so an unknown ownership path remains the
    # conservative first choice when callers provide an explicit candidate order.
    selected: list[Assignment] = []
    for task in candidates:
        if len(selected) >= limit:
            break
        if not any(_conflict(task.paths, other.paths) for other in selected):
            selected.append(task)
    return selected
