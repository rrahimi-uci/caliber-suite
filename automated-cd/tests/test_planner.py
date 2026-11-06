from autocd.planner import Assignment, assign, eligible


def test_ready_eligibility_and_dependency_policy(snapshot):
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
    assert [task.issue for task in tasks] == [12, 13, 14, 18]
    assert tasks[0].paths == frozenset({"src/a.py"})
    assert [task.issue for task in eligible(data, allowlist=(18,))] == [18]


def test_assign_serializes_unknown_and_overlapping_paths():
    def task(n, paths):
        return Assignment(n, str(n), "", "release:Beta", frozenset(paths), 1)

    candidates = [task(1, {"src/a.py"}), task(2, {"src/b.py"}), task(3, {"src"}), task(4, set())]
    assert [t.issue for t in assign(candidates, 4, 4)] == [1, 2]
    assert [t.issue for t in assign([task(4, set()), *candidates], 4, 4)] == [4]
    assert [t.issue for t in assign(candidates, 1, 4)] == [1]


def test_milestone_target_and_priority(snapshot):
    data, item = snapshot
    data["items"] = [item(7, fields={"Release": "", "Priority": "P0"})]
    assert eligible(data)[0].target == "milestone:#4"
