"""The documented way to run the conformance suite against an adapter that is not one of the shipped four (B08.5, #769).

``toy_adapter.py`` is the example the guide in ``docs/operate/reference-profile.md`` quotes: an adapter for a made-up provider that
shares nothing with the shipped adapters. This module runs it exactly as the guide says to, and keeps the guide honest: the quoted
``build`` is the file's, every rule is in the table, and the counts the guide states are the real ones.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from caliber.testing.adapter_conformance import RULES, Rule, check_rule, conformance_report
from tests.conformance.broken_adapters import MUTANTS
from tests.conformance.toy_adapter import toy_subject

SUBJECT = toy_subject()
GUIDE = Path(__file__).resolve().parents[3] / "docs" / "operate" / "reference-profile.md"
EXAMPLE = Path(__file__).resolve().parent / "toy_adapter.py"


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.id)
def test_acme_conforms(rule: Rule) -> None:
    """The guide's own test, with the example adapter for ``acme``."""
    check_rule(SUBJECT, rule.id)


def test_the_report_of_the_example_adapter_is_a_pass() -> None:
    report = conformance_report(SUBJECT)
    assert report.passed and report.failed == () and report.unsupported == ()


def _guide_section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index("## Adapter conformance suite: the rules every provider adapter passes")
    return text[start : text.index("\n## ", start + 1)]


@pytest.mark.parametrize("region", ["imports", "adapter", "build"])
def test_the_code_the_guide_quotes_is_the_examples(region: str) -> None:
    quoted = re.search(rf"# --8<-- {region}\n(.*?)# --8<-- end {region}", EXAMPLE.read_text(), re.S)
    assert quoted is not None
    assert quoted.group(1).rstrip() + "\n" in _guide_section(), (
        f"the guide's {region} have drifted from toy_adapter.py"
    )


def test_every_rule_is_in_the_guides_table_with_its_title() -> None:
    section = _guide_section()
    for rule in RULES:
        assert f"| `{rule.id}` | {rule.title[0].upper() + rule.title[1:]} |" in section, rule.id


def test_the_counts_the_guide_states_are_the_real_ones() -> None:
    section = _guide_section()
    assert f"4 adapters x {len(RULES)} rules" in section
    assert f"holds {len(MUTANTS)} deliberately broken adapters" in section
    assert f"passes all {len(RULES)}" in section
    optional = ", ".join(f"`{rule.id}`" for rule in RULES if rule.optional)
    assert f"({optional})" in section


def test_the_guides_example_runs_as_a_module_on_its_own(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipe is self-contained: the code block, executed with nothing but ``caliber`` installed, defines the adapter and passes."""
    section = _guide_section()
    block = section.split("```python\n", 1)[1].split("\n```", 1)[0]
    module = types.ModuleType("acme_adapter_test")
    monkeypatch.setitem(
        sys.modules, module.__name__, module
    )  # its dataclasses look their module up
    namespace = module.__dict__
    exec(compile(block, "reference-profile.md", "exec"), namespace)
    assert "tests.conformance" not in block and "build_acme_adapter" not in block
    for rule in RULES:
        namespace["test_acme_conforms"](rule)  # type: ignore[operator]
    assert capsys.readouterr().out.rstrip().endswith("CONFORMS")


def test_the_failure_the_guide_shows_is_what_the_suite_prints() -> None:
    mutant = next(m for m in MUTANTS if m.name == "RetriesTheProviderOnce")
    printed = [
        line
        for line in conformance_report(mutant.subject()).render().splitlines()
        if "PASS" not in line
    ]
    section = _guide_section()
    assert "\n".join(printed) in section, "the guide's failure example is not the suite's output"
    assert f"{printed[-1].split('(')[1].split(' ')[0]} of its {len(RULES)} rules failed" in section
