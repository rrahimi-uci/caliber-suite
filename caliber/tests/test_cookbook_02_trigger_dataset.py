"""Cookbook 02 (Precision Skills): the shipped dataset, scored by the real skill selector.

The lab README tells the reader what the trigger test set does to the skill: the starter skill
(only six exclusion phrases) decides 30 of 40 cases correctly, and with the tightened phrases it
decides all 40 correctly. Those numbers are claims about the real deterministic selector, so this
test pins them through the same routes the page and the example script call, offline.

An earlier audit found the shipped negative cases T10-T12 selecting the skill (the summary names
engineering words only to exclude them, and the selector counts any summary word that matches the
query); this is the regression guard for that fix, and for the dataset staying honest as the
selector changes.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.testclient import TestClient

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LAB = (
    Path(__file__).resolve().parents[2]
    / "docs-site/cookbooks/02-skill-trigger-packaging-lab/assets"
)

# What the starter phrases get wrong, as the README states it (with artifact_type/session_goal).
ROUND1_FALSE_POSITIVES = ["T27", "T28", "T29", "T30", "T31", "T32", "T34", "T35"]
ROUND1_FALSE_NEGATIVES = ["T14", "T23"]


def _skill_spec() -> tuple[dict[str, Any], str]:
    text = (LAB / "skills/support-tone-and-citation.md").read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body.strip()


def _cases() -> list[dict[str, Any]]:
    lines = (LAB / "dataset/trigger-cases.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _tightened() -> dict[str, Any]:
    path = LAB / "skills/trigger-phrases.json"
    triggers: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["test_triggers"]
    return triggers


def _create(client: TestClient, name: str, triggers: dict[str, Any] | None = None) -> str:
    spec, body = _skill_spec()
    triggers = triggers or {"should_not_trigger": spec["should_not_trigger"]}
    response = client.post(
        f"{PREFIX}/skills",
        json={
            "name": name,
            "owner": "admin",
            "summary": spec["summary"],
            "category": spec["category"],
            "tags": spec["tags"],
            "content": body,
            "skill_metadata": {"test_triggers": triggers},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["skill_id"])


def _decide(client: TestClient, skill_id: str, case: dict[str, Any], *, context: bool) -> bool:
    inputs = case["inputs"] if context else {"user_message": case["inputs"]["user_message"]}
    response = client.post(f"{PREFIX}/skills/{skill_id}/test-selection", json=inputs)
    assert response.status_code == 200, response.text
    return bool(response.json()["data"]["is_selected"])


def _misses(client: TestClient, skill_id: str, *, context: bool) -> tuple[list[str], list[str]]:
    false_positives, false_negatives = [], []
    for case in _cases():
        selected = _decide(client, skill_id, case, context=context)
        if selected != case["expectations"]["is_selected"]:
            (false_positives if selected else false_negatives).append(case["id"])
    return false_positives, false_negatives


def test_dataset_shape() -> None:
    cases = _cases()
    assert len(cases) == 40
    assert [case["id"] for case in cases] == [f"T{n:02d}" for n in range(1, 41)]
    positives = [c for c in cases if c["expectations"]["is_selected"]]
    negatives = [c for c in cases if not c["expectations"]["is_selected"]]
    assert len(positives) == 19 and len(negatives) == 21
    assert {t for c in cases for t in c["tags"]} == {"positive", "negative", "near-miss", "edge"}
    assert all("user_message" in c["inputs"] for c in cases)
    near_misses = [c for c in cases if "near-miss" in c["tags"]]
    assert len(near_misses) >= 8 and not any(c["expectations"]["is_selected"] for c in near_misses)


def test_the_original_negative_cases_do_not_select_the_starter_skill(client: TestClient) -> None:
    """T07-T12 were the audit finding: before the should_not_trigger phrases, T10-T12 selected."""
    skill_id = _create(client, "cookbook-02-starter-negatives")
    originals = [case for case in _cases() if case["id"] in {f"T{n:02d}" for n in range(1, 13)}]
    for case in originals:
        for context in (True, False):
            assert (
                _decide(client, skill_id, case, context=context)
                == (case["expectations"]["is_selected"])
            ), (case["id"], context)


def test_round_one_with_the_starter_phrases_misses_what_the_readme_says(
    client: TestClient,
) -> None:
    skill_id = _create(client, "cookbook-02-round-one")
    false_positives, false_negatives = _misses(client, skill_id, context=True)
    assert false_positives == ROUND1_FALSE_POSITIVES
    assert false_negatives == ROUND1_FALSE_NEGATIVES


def test_round_two_with_the_tightened_phrases_is_clean_with_and_without_context(
    client: TestClient,
) -> None:
    skill_id = _create(client, "cookbook-02-round-two", _tightened())
    assert _misses(client, skill_id, context=True) == ([], [])
    # the Scenario Sets runner on the Skills page sends the message alone
    assert _misses(client, skill_id, context=False) == ([], [])


def test_a_positive_phrase_never_beats_an_exclusion(client: TestClient) -> None:
    """An exclusion match scores 0 even when a positive phrase and the tags also match."""
    skill_id = _create(client, "cookbook-02-exclusion-wins", _tightened())
    case = next(c for c in _cases() if c["id"] == "T30")  # "invoice" (positive) + "pull request"
    response = client.post(
        f"{PREFIX}/skills/{skill_id}/test-selection", json=case["inputs"]
    ).json()["data"]
    assert response["is_selected"] is False
    assert response["selection_score"] == 0
    assert response["selection_reason"] == "excluded:negative_trigger:pull request"


def test_render_preview_matches_the_readme(client: TestClient) -> None:
    skill_id = _create(client, "cookbook-02-render")
    render = json.loads((LAB / "dataset/render-vars.json").read_text(encoding="utf-8"))
    full = client.post(
        f"{PREFIX}/skills/{skill_id}/test-render", json={"variables": render["variables"]}
    ).json()["data"]
    assert full["unresolved_variables"] == render["expected"]["unresolved_variables"] == []
    assert full["detected_variables"] == ["policy_context", "audience", "user_message"]
    assert "[KB-1042]" in full["rendered_content"]
    assert not re.search(r"\{\{\s*\w+\s*\}\}", full["rendered_content"])

    partial = client.post(
        f"{PREFIX}/skills/{skill_id}/test-render",
        json={"variables": render["partial"]["variables"]},
    ).json()["data"]
    assert partial["unresolved_variables"] == render["partial"]["expected"]["unresolved_variables"]
    assert "{{ policy_context }}" in partial["rendered_content"]


@pytest.mark.parametrize("item", json.loads((LAB / "skills/invalid-skills.json").read_text()))
def test_invalid_skills_are_rejected_with_the_documented_reason(
    client: TestClient, item: dict[str, Any]
) -> None:
    response = client.post(f"{PREFIX}/skills", json=item["payload"])
    assert response.status_code == item["status"], response.text
    assert item["expect"] in response.text, (item["label"], response.text)
    assert client.get(f"{PREFIX}/skills").json()["data"] == []


def test_package_round_trip_keeps_the_instructions_but_not_the_selection_metadata(
    client: TestClient,
) -> None:
    """Export then import under a new name: the package carries name, description, content and the
    OpenAI interface, so tags, category and trigger phrases come back empty and the copy decides
    some cases differently until they are restored. The README states exactly this."""
    spec, _ = _skill_spec()
    skill_id = _create(client, "cookbook-02-source", _tightened())
    archive = client.get(f"{PREFIX}/skills/{skill_id}/package.zip")
    assert archive.status_code == 200
    names = sorted(zipfile.ZipFile(io.BytesIO(archive.content)).namelist())
    assert names == ["cookbook-02-source/SKILL.md", "cookbook-02-source/agents/openai.yaml"]

    def upload(strategy: str, rename_to: str = "") -> Any:
        return client.post(
            f"{PREFIX}/skills/import-package.zip",
            data={"conflict_strategy": strategy, "rename_to": rename_to},
            files={"file": ("cookbook-02-source.zip", archive.content, "application/zip")},
        )

    assert upload("reject").status_code == 409
    imported = upload("rename", "cookbook-02-copy")
    assert imported.status_code == 201, imported.text
    copy = imported.json()["data"]
    source = client.get(f"{PREFIX}/skills/{skill_id}").json()["data"]
    assert copy["content"] == source["content"]
    assert copy["description"] == spec["summary"], "an empty description falls back to the summary"
    assert copy["tags"] == [] and copy["category"] == "custom"
    assert "test_triggers" not in copy["skill_metadata"]
    assert copy["summary"].endswith("...") and len(copy["summary"]) <= 64

    before = _misses(client, copy["skill_id"], context=True)
    assert before != ([], []), (
        "the copy cannot match the source until its selection metadata is back"
    )

    restored = client.patch(
        f"{PREFIX}/skills/{copy['skill_id']}",
        json={
            "tags": spec["tags"],
            "category": spec["category"],
            "summary": spec["summary"],
            "skill_metadata": {**copy["skill_metadata"], "test_triggers": _tightened()},
        },
    )
    assert restored.status_code == 200, restored.text
    assert _misses(client, copy["skill_id"], context=True) == ([], [])
