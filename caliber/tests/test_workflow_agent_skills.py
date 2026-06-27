"""Skills on workflow agents: manifest → compiler → runtime → codegen → validation.

A skill's content is resolved at compile time and composed into the agent's
system prompt as a labelled block, both in the in-server runtime and the
exported Agents-SDK code (parity).
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from caliber.workflows.compiler import build_ir, generate_python
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import _agent_instruction_text
from caliber.workflows.validation import validate_manifest
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_manifest,
    registry_resolver,
)


def _agent_skills_manifest(
    workflow_id: str = "wf", skills: tuple[str, ...] = ("tone",)
) -> dict[str, Any]:
    raw = make_manifest(workflow_id)
    raw["nodes"]["agent"]["skills"] = list(skills)
    return raw


def test_manifest_accepts_agent_skills() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("tone", "safety")))
    assert m.nodes["agent"].skills == ["tone", "safety"]


def test_skill_free_agent_defaults_to_empty() -> None:
    m = parse_manifest(make_manifest("wf"))
    assert m.nodes["agent"].skills == []


def test_compiler_composes_skill_content_into_ir() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("tone", "safety")))
    ir = build_ir(
        m,
        registry_resolver(),
        skill_contents={"tone": "Be concise.", "safety": "Refuse harmful asks."},
    )
    assert ir.nodes["agent"].skill_instructions == ["Be concise.", "Refuse harmful asks."]


def test_compiler_skips_unresolved_skills() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("tone", "missing")))
    ir = build_ir(m, registry_resolver(), skill_contents={"tone": "Be concise."})
    assert ir.nodes["agent"].skill_instructions == ["Be concise."]


def test_runtime_composes_skills_into_system_prompt() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("tone",)))
    ir = build_ir(m, registry_resolver(), skill_contents={"tone": "Be concise."})
    text = _agent_instruction_text(ir.nodes["agent"])
    assert "You are helpful." in text  # base inline instructions preserved
    assert "## Skill" in text
    assert "Be concise." in text


def test_generated_python_includes_skill_blocks() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("tone",)))
    ir = build_ir(m, registry_resolver(), skill_contents={"tone": "Be concise."})
    code = generate_python(ir)
    # Exported code composes skills too (runtime/export parity).
    assert "## Skill" in code
    assert "Be concise." in code


def test_cache_key_varies_with_skill_content() -> None:
    from caliber.workflows.compiler import _cache_key

    m = parse_manifest(_agent_skills_manifest(skills=("tone",)))
    resolver = registry_resolver()
    k1 = _cache_key(m, resolver, "1", {"tone": "Be concise."})
    k2 = _cache_key(m, resolver, "1", {"tone": "Be VERY concise."})
    assert k1 != k2  # editing a skill busts the compile cache


def test_validation_flags_unknown_skill_ref() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("ghost",)))
    report = validate_manifest(m, resolver=registry_resolver(), skill_names={"tone", "safety"})
    assert not report.valid
    assert any(i.code == "missing_skill_ref" for i in report.errors)


def test_validation_passes_for_known_skill() -> None:
    m = parse_manifest(_agent_skills_manifest(skills=("tone",)))
    report = validate_manifest(m, resolver=registry_resolver(), skill_names={"tone"})
    assert all(i.code != "missing_skill_ref" for i in report.errors)


def test_validation_skips_skill_check_without_skill_names() -> None:
    # The compile path passes no skill_names → skills aren't enforced.
    m = parse_manifest(_agent_skills_manifest(skills=("ghost",)))
    report = validate_manifest(m, resolver=registry_resolver())
    assert all(i.code != "missing_skill_ref" for i in report.errors)


def test_validate_route_flags_unknown_skill(client: TestClient) -> None:
    wid = create_workflow(client)
    vid, _ = create_draft(client, wid, _agent_skills_manifest(wid, skills=("ghost",)))
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert r.status_code == 200, r.text
    report = r.json()["data"]
    assert any(e["code"] == "missing_skill_ref" for e in report["errors"]), report
