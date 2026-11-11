import json

import pytest

from autocd.planner import Assignment, assign, completed_today, eligible, view_label


def _metadata(paths):
    return f'<!-- caliber-engineering-meta:{json.dumps({"owned_paths": paths})} -->'


def test_executable_eligibility_and_dependency_policy(snapshot):
    data, item = snapshot
    data["items"] += [
        item(15, issue={"blockedBy": [{"state": "CLOSED", "stateReason": "NOT_PLANNED"}]}),
        item(16, issue={"blockedBy": [{"state": "OPEN", "stateReason": None}]}),
        item(17, fields={"Human input": "H01: start"}),
        item(18, fields={"Human input": "H02: acceptance"}),
        item(19, issue={"assignees": ["someone"]}),
        item(20, issue={"prs": [{"state": "OPEN"}]}),
        item(21, fields={"Readiness": "Planned"}),
        item(22, fields={"Item type": "Epic"}),
        item(23, fields={"Release": ""}, issue={"milestone": {}}),
        item(24, isArchived=True),
        item(25, issue={"state": "CLOSED"}),
        item(26, issue={"repository": "other/repo"}),
    ]
    tasks = eligible(data, pathmap={12: frozenset({"src/a.py"})})
    assert [task.issue for task in tasks] == [12, 13, 14, 18, 21]
    assert tasks[0].paths == frozenset({"src/a.py"})
    assert tasks[-1].readiness == "Planned"
    assert [task.issue for task in eligible(data, allowlist=(18,))] == [18]


def test_hosted_eligibility_requires_valid_declared_ownership(snapshot):
    data, item = snapshot
    data["items"] = [
        item(12, issue={"body": _metadata(["src/task.py", "tests/test_task.py"])}),
        item(13, issue={"body": "an audit with no machine-readable scope"}),
        item(14, issue={"body": _metadata([])}),
        item(15, issue={"body": _metadata(["../outside"])}),
    ]

    tasks = eligible(data, require_owned_paths=True)

    assert [task.issue for task in tasks] == [12]
    assert tasks[0].paths == frozenset({"src/task.py", "tests/test_task.py"})


def test_configured_issue_paths_override_missing_issue_metadata(snapshot):
    data, item = snapshot
    data["items"] = [item(12, issue={"body": "legacy task without metadata"})]

    tasks = eligible(
        data,
        pathmap={12: frozenset({"src/task.py"})},
        require_owned_paths=True,
    )

    assert [task.issue for task in tasks] == [12]
    assert tasks[0].paths == frozenset({"src/task.py"})


def test_assign_serializes_unknown_and_overlapping_paths():
    def task(n, paths):
        return Assignment(n, str(n), "", "release:Beta", frozenset(paths), 1)

    candidates = [task(1, {"src/a.py"}), task(2, {"src/b.py"}), task(3, {"src"}), task(4, set())]
    assert [t.issue for t in assign(candidates, 4, 4)] == [1, 2]
    assert [t.issue for t in assign([task(4, set()), *candidates], 4, 4)] == [4]
    assert [t.issue for t in assign(candidates, 1, 4)] == [1, 2]


def test_assign_uses_the_earliest_execution_order_bucket():
    def task(number, order, paths, priority=1):
        return Assignment(
            number,
            str(number),
            "",
            "release:Beta",
            frozenset(paths),
            priority,
            order,
        )

    candidates = [
        task(10, 1000, {"src/10"}, priority=0),
        task(20, 1000, {"src/20"}, priority=1),
        task(30, 3000, {"src/30"}, priority=0),
    ]
    assert [t.issue for t in assign(candidates, 4, 4)] == [10, 20]


def test_missing_execution_order_is_a_fallback_after_ordered_work(snapshot):
    data, item = snapshot
    data["items"] = [
        item(12, fields={"Execution order": None, "Priority": "P0"}),
        item(13, fields={"Execution order": 2000, "Priority": "P3"}),
    ]
    tasks = eligible(data)
    assert [task.issue for task in tasks] == [13, 12]
    assert [task.issue for task in assign(tasks, 1, 4)] == [13]

    data["items"] = [item(12, fields={"Execution order": None, "Priority": "P0"})]
    assert [task.issue for task in assign(eligible(data), 1, 4)] == [12]

    data["items"] = [item(12, fields={"Execution order": 2000.0})]
    assert eligible(data)[0].execution_order == 2000
    data["items"] = [item(12, fields={"Execution order": True})]
    assert eligible(data)[0].execution_order is None


def test_milestone_target_and_priority(snapshot):
    data, item = snapshot
    data["items"] = [item(7, fields={"Release": "", "Priority": "P0"})]
    assert eligible(data)[0].target == "milestone:#4"


def test_view_scope_filters_the_live_release(snapshot):
    data, item = snapshot
    assert view_label(data, 0) is None
    data["view"] = {"number": 6, "name": "Beta", "filter": "is:open label:release:beta"}
    label = view_label(data, 6)
    assert label == "release:beta"
    data["items"].append(item(27, issue={"labels": ["release:preview-1"]}))
    assert [task.issue for task in eligible(data, scope_label=label)] == [12, 13, 14]
    with pytest.raises(ValueError, match="does not match"):
        view_label(data, 7)
    data["view"]["filter"] = "is:open label:release:beta priority:high"
    with pytest.raises(ValueError, match="filter changed"):
        view_label(data, 6)


def test_daily_done_requires_project_done_completed_issue_and_merged_pr(snapshot):
    data, item = snapshot

    def done(number, **overrides):
        issue = {
            "state": "CLOSED",
            "stateReason": "COMPLETED",
            "prs": [{"state": "MERGED", "mergedAt": "2026-11-05T08:00:00Z"}],
        }
        issue.update(overrides.pop("issue", {}))
        return item(
            number,
            fields={"Status": "Done", "Readiness": "Done", **overrides.pop("fields", {})},
            issue=issue,
        )

    data["items"] = [
        done(30),
        done(31, issue={"prs": [{"state": "MERGED", "mergedAt": "2026-11-05T07:59:59Z"}]}),
        done(32, issue={"prs": []}),
        done(33, issue={"labels": ["release:other"]}),
        done(34, fields={"Item type": "Epic"}),
        done(35, fields={"Readiness": "In Review"}),
        done(36, issue={"stateReason": "NOT_PLANNED"}),
    ]
    closed = [
        {"number": 30, "closedAt": "2026-11-05T08:00:00Z", "stateReason": "COMPLETED"},
        {"number": 31, "closedAt": "2026-11-05T07:59:59Z", "stateReason": "COMPLETED"},
        *(
            {"number": n, "closedAt": "2026-11-05T12:00:00Z", "stateReason": "COMPLETED"}
            for n in range(32, 37)
        ),
    ]
    assert completed_today(data, closed, "release:beta", "2026-11-05", "America/Los_Angeles") == [
        30
    ]
    assert completed_today(data, closed, None, "2026-11-05", "America/Los_Angeles") == [
        30,
        33,
    ]


def test_daily_done_uses_later_of_merge_and_issue_completion(snapshot):
    data, item = snapshot
    data["items"] = [
        item(
            30,
            fields={"Status": "Done", "Readiness": "Done"},
            issue={
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "prs": [{"state": "MERGED", "mergedAt": "2026-11-05T08:01:00Z"}],
            },
        )
    ]
    closed = [{"number": 30, "closedAt": "2026-11-05T07:59:00Z", "stateReason": "COMPLETED"}]
    assert completed_today(data, closed, "release:beta", "2026-11-04", "America/Los_Angeles") == []
    assert completed_today(data, closed, "release:beta", "2026-11-05", "America/Los_Angeles") == [
        30
    ]
