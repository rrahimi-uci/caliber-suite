"""Contract checks for the B16.1 beta fault matrix."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT.parent / "docs" / "reports" / "egi" / "beta-fault-matrix.md"
EXPECTED_IDS = tuple(f"V{number:02d}" for number in range(1, 15))


def _cells(line: str) -> list[str]:
    assert line.startswith("|") and line.endswith("|"), f"not a markdown table row: {line!r}"
    return [cell.strip() for cell in line[1:-1].split("|")]


def _parse_matrix(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    header_index = next(
        index for index, line in enumerate(lines) if line.startswith("| ID | Injection")
    )
    headers = _cells(lines[header_index])
    assert headers == [
        "ID",
        "Injection",
        "Status",
        "Pytest evidence",
        "Offline oracle",
        "Boundary / remaining evidence",
    ]
    assert header_index + 1 < len(lines) and lines[header_index + 1].startswith("|---")
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("| V"):
            if rows:
                break
            continue
        values = _cells(line)
        assert len(values) == len(headers), f"wrong column count for {values[0]!r}"
        rows.append(dict(zip(headers, values, strict=True)))
    return rows


def _nodeids(rows: list[dict[str, str]]) -> list[str]:
    nodeids: list[str] = []
    for row in rows:
        reference = row["Pytest evidence"].strip("`")
        assert "::" in reference, f"{row['ID']} must name a pytest file::case"
        nodeid = reference.removeprefix("caliber/")
        assert nodeid.startswith("tests/")
        nodeids.append(nodeid)
    return nodeids


def test_matrix_has_one_automated_row_per_injection_and_every_case_collects() -> None:
    rows = _parse_matrix(MATRIX.read_text(encoding="utf-8"))
    assert [row["ID"] for row in rows] == list(EXPECTED_IDS)
    assert all(row["Status"] == "automated" for row in rows)
    nodeids = _nodeids(rows)

    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, environment.get("PYTHONPATH", "")) if part
    )
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cov",
            "-o",
            "addopts=",
            *nodeids,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    for nodeid in nodeids:
        assert nodeid in collected.stdout, f"pytest did not collect {nodeid}\n{collected.stdout}"


def test_matrix_parser_rejects_a_missing_or_non_automated_contract_row() -> None:
    valid = MATRIX.read_text(encoding="utf-8")
    rows = _parse_matrix(valid)
    assert len(rows) == len(EXPECTED_IDS)
    assert rows[0]["Status"] == "automated"

    malformed = valid.replace("| V01 |", "| V00 |", 1)
    with pytest.raises(AssertionError):
        parsed = _parse_matrix(malformed)
        assert [row["ID"] for row in parsed] == list(EXPECTED_IDS)

    non_automated = valid.replace("| automated |", "| partial |", 1)
    with pytest.raises(AssertionError):
        parsed = _parse_matrix(non_automated)
        assert all(row["Status"] == "automated" for row in parsed)
