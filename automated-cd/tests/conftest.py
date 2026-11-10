from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from autocd.config import load


@pytest.fixture
def settings(tmp_path: Path):
    return replace(
        load(),
        state_dir=tmp_path / "private-state",
        enabled=False,
        auto_merge=False,
        # Existing lifecycle tests model one developer turn per review cycle; the
        # fix loop has its own tests.
        max_fix_attempts=0,
        setup_commands=(),
        issue_checks={12: ("python -m pytest tests/test_task.py --cov=task --cov-fail-under=95",)},
        issue_paths={12: frozenset({"src/task.py", "tests/test_task.py"})},
    )


@pytest.fixture
def snapshot():
    def item(number: int, **overrides):
        issue = {
            "number": number,
            "title": f"Task {number}",
            "body": "contract",
            "state": "OPEN",
            "repository": "rrahimi-uci/caliber-suite",
            "assignees": [],
            "labels": ["release:beta"],
            "prs": [],
            "blockedBy": [],
            "milestone": {"number": 4},
        }
        issue.update(overrides.pop("issue", {}))
        fields = {
            "Status": "Todo",
            "Readiness": "Ready",
            "Item type": "Task",
            "Human input": "None pending",
            "Priority": "P1",
            "Release": "Beta",
            "Execution order": 1000,
        }
        fields.update(overrides.pop("fields", {}))
        return {
            "id": f"item-{number}",
            "issue": issue,
            "fields": fields,
            "isArchived": overrides.pop("isArchived", False),
        }

    return {
        "repository": {"nameWithOwner": "rrahimi-uci/caliber-suite", "defaultBranch": "main"},
        "view": {"number": 0, "name": "All Project Tasks", "filter": ""},
        "items": [item(12), item(13), item(14)],
    }, item
