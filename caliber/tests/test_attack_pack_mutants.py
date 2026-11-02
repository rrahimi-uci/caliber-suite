"""The mutant table of the attack pack (``scripts/attack_pack_mutants.py``, B12.4c, #770) stays true to the source it mutates.

Running every mutant takes minutes and PostgreSQL (``python scripts/attack_pack_mutants.py``); what runs here, offline and in
a second, is what keeps that check honest: every edit's anchor still exists exactly once (so a refactor of the refusal code
cannot silently turn a mutant into a no-op that "survives" or "is killed" for the wrong reason), and every mutated file still
parses (a mutant that does not import would count as killed without proving anything).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "attack_pack_mutants.py"
MINIMUM_MUTANTS = 10


@pytest.fixture(scope="module")
def mutants_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("attack_pack_mutants", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_there_are_at_least_ten_mutants_each_with_a_reason(mutants_module: ModuleType) -> None:
    mutants = mutants_module.MUTANTS
    assert len(mutants) >= MINIMUM_MUTANTS
    assert all(m.why.strip() and m.edits for m in mutants)


def test_every_anchor_exists_exactly_once_and_every_edit_changes_something(
    mutants_module: ModuleType,
) -> None:
    assert mutants_module.check_anchors() == []


def test_every_mutated_file_still_parses(mutants_module: ModuleType) -> None:
    for mutant in mutants_module.MUTANTS:
        for edit in mutant.edits:
            source = (mutants_module.SRC / edit.path).read_text(encoding="utf-8")
            ast.parse(source.replace(edit.old, edit.new), filename=f"{mutant.name}:{edit.path}")


def test_a_broken_anchor_is_reported(
    mutants_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit = mutants_module.Edit("csrf.py", "this text is not in csrf.py", "x")
    monkeypatch.setattr(mutants_module, "MUTANTS", (mutants_module.Mutant("gone", "why", (edit,)),))
    (problem,) = mutants_module.check_anchors()
    assert "found 0 times" in problem
