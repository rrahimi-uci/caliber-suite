"""The gate ledger must name gates that exist.

``.github/scripts/gate_ledger.py`` fails a run that would otherwise report success
while a required gate produced no evidence. It identifies gates by job name, which
means a renamed job silently becomes a gate that "did not run" -- loud, but only
after a push, and loud in a way that invites deleting the entry rather than fixing
it.

These tests close that loop locally: the ledger's list and the workflow's job names
are checked against each other, and the matrix legs are expanded the way GitHub
expands them. The list stays hand-written on purpose -- discovering it from the
workflow would make it agree with any workflow, including one that lost a gate.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LEDGER = REPO_ROOT / ".github" / "scripts" / "gate_ledger.py"

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the workflow")


def _ledger_module():
    spec = importlib.util.spec_from_file_location("gate_ledger", LEDGER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job_names() -> dict[str, str]:
    """Every job name the workflow can produce, mapped back to its job key.

    Matrix legs are expanded the way GitHub expands them, so
    ``Compatibility (Python ${{ matrix.python-version }})`` becomes one entry per
    declared version -- which is how the names appear in the API the ledger reads.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for key, job in workflow["jobs"].items():
        template = job.get("name", key)
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        placeholders = re.findall(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", template)
        if not placeholders:
            names[template] = key
            continue
        for placeholder in placeholders:
            for value in matrix.get(placeholder, []):
                expanded = re.sub(
                    rf"\$\{{\{{\s*matrix\.{placeholder}\s*\}}\}}", str(value), template
                )
                names[expanded] = key
    return names


def test_every_required_gate_names_a_job_the_workflow_defines() -> None:
    required = set(_ledger_module().REQUIRED_GATES)
    unknown = required - set(_job_names())

    assert not unknown, (
        "gate_ledger.py requires gates no job produces, so every run would report "
        f"them as missing evidence: {sorted(unknown)}. Rename them here, or drop "
        "them if the gate is genuinely gone."
    )


def test_the_ledger_waits_for_every_gate_it_reports_on() -> None:
    """Reading a conclusion before the job has one yields ``None``.

    The ledger reads ``None`` as "did not run", so a gate it reports on but does
    not wait for would produce a false alarm on every run -- which trains readers
    to ignore the job, recreating the silence it exists to break.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    waited_for = set(workflow["jobs"]["gate-ledger"].get("needs") or [])
    by_name = _job_names()

    unwaited = {
        gate for gate in _ledger_module().REQUIRED_GATES if by_name.get(gate) not in waited_for
    }

    assert not unwaited, (
        f"gate-ledger reports on gates it does not wait for: {sorted(unwaited)}. "
        "Add their job keys to its `needs:`."
    )


def test_the_package_gate_is_not_sequenced_behind_unrelated_jobs() -> None:
    """The wheel/SPA check must not be reachable only when everything else passes.

    This job asserts the wheel contains a bundled SPA. It was skipped on twelve
    consecutive runs because it sat behind six unrelated jobs, and the first time
    it ran it failed. A wheel with no SPA is broken whether or not the tests pass,
    so sequencing it behind them removes evidence exactly when it is most wanted.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    needs = set(workflow["jobs"]["package"].get("needs") or [])

    assert needs <= {"ui"}, (
        f"the wheel build now waits on {sorted(needs)}. Every entry beyond 'ui' "
        "(whose dist artifact it consumes) is a job whose failure silently skips "
        "this gate rather than failing it."
    )
