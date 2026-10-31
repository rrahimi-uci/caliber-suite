"""The documented way to run the conformance suite against an adapter that is not one of the shipped four (B08.5, #769).

``toy_adapter.py`` is the example the guide in ``docs/operate/reference-profile.md`` quotes: an adapter for a made-up provider that
shares nothing with the shipped adapters. This module runs it exactly as the guide says to, and keeps the guide honest: the quoted
``build`` is the file's, every rule is in the table, and the counts the guide states are the real ones.
"""

from __future__ import annotations

import re
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


def test_the_build_the_guide_quotes_is_the_examples() -> None:
    quoted = re.search(r"# --8<-- build\n(.*?)# --8<-- end build", EXAMPLE.read_text(), re.S)
    assert quoted is not None
    assert quoted.group(1).rstrip() + "\n" in _guide_section(), (
        "the guide's example has drifted from toy_adapter.py"
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
    assert f"two of its {len(RULES)} rules failed" in section
