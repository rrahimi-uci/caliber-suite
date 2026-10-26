"""The architecture decision records are indexed, well-formed and cover every protected path.

``docs/architecture/adr/README.md`` lists every ``0*.md`` record with its status. Each record has the
headings ``## Status``, ``## Context``, ``## Decision`` and ``## Consequences`` in that order, and the status
the index shows is the status the record opens with. ADR 0002 owns every protected path: each row id of
``docs/architecture/protected-effect-paths.md`` appears in its ownership table, so a path cannot be added to
the inventory without an owner.

The tests read repository files and skip when run from an sdist without the docs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = REPO / "docs" / "architecture" / "adr"
INVENTORY = REPO / "docs" / "architecture" / "protected-effect-paths.md"

pytestmark = pytest.mark.skipif(not ADR_DIR.is_dir(), reason="repository docs not present")

REQUIRED_HEADINGS = ("## Status", "## Context", "## Decision", "## Consequences")
STATUSES = ("Proposed", "Accepted", "Superseded", "Rejected")
ROW_ID = re.compile(r"\bEP-\d{3}\b")


def adr_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("0*.md"))


def indexed_rows(index_text: str) -> dict[str, str]:
    """``{file name: status cell}`` for each index row that links to a record."""
    rows: dict[str, str] = {}
    for line in index_text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        link = re.match(r"\[(\d{4})\]\(([^)]+\.md)\)", cells[0]) if len(cells) == 3 else None
        if link:
            rows[link.group(2)] = cells[2]
    return rows


def headings_problem(text: str) -> str | None:
    """Why ``text`` is not a well-formed record, or ``None``."""
    found = [line.strip() for line in text.splitlines() if line.startswith("## ")]
    present = [h for h in found if h in REQUIRED_HEADINGS]
    missing = [h for h in REQUIRED_HEADINGS if h not in present]
    if missing:
        return f"missing {missing}"
    if present != list(REQUIRED_HEADINGS):
        return f"headings are {present}, expected {list(REQUIRED_HEADINGS)} in that order"
    return None


def status_of(text: str) -> str | None:
    """The first word of the record's ``## Status`` section when it is a known status."""
    match = re.search(r"^## Status\s+(\w+)", text, re.M)
    return match.group(1) if match and match.group(1) in STATUSES else None


def check_adr_directory(directory: Path) -> list[str]:
    """Every problem with the records in ``directory``; empty when it is consistent."""
    index_path = directory / "README.md"
    if not index_path.exists():
        return [f"{index_path} is missing"]
    indexed = indexed_rows(index_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for path in adr_files(directory):
        text = path.read_text(encoding="utf-8")
        if path.name not in indexed:
            problems.append(f"{path.name} is not listed in README.md")
        if (problem := headings_problem(text)) is not None:
            problems.append(f"{path.name}: {problem}")
        status = status_of(text)
        if status is None:
            problems.append(f"{path.name}: its Status section does not open with one of {STATUSES}")
        elif path.name in indexed and indexed[path.name] != status:
            problems.append(
                f"{path.name}: README.md says {indexed[path.name]!r}, the record says {status!r}"
            )
    for name in indexed:
        if not (directory / name).exists():
            problems.append(f"README.md links to {name}, which does not exist")
    return problems


def inventory_row_ids(text: str) -> list[str]:
    return [
        m.group(0)
        for line in text.splitlines()
        if line.startswith("| EP-")
        for m in [ROW_ID.search(line)]
        if m
    ]


def ownership_table_rows(adr_text: str) -> list[str]:
    """The row ids in ADR 0002's protected-path table (the table after ``### Protected paths``)."""
    section = adr_text.split("### Protected paths", 1)[1]
    section = re.split(r"^## ", section, maxsplit=1, flags=re.M)[0]
    return [
        m.group(0)
        for line in section.splitlines()
        if line.startswith("| EP-")
        for m in [ROW_ID.match(line[2:])]
        if m
    ]


def uncovered(adr_text: str, ids: list[str]) -> list[str]:
    owned = set(ownership_table_rows(adr_text))
    return [row_id for row_id in ids if row_id not in owned]


# ---------------------------------------------------------------- the real records


def test_every_adr_is_indexed_and_has_required_headings() -> None:
    assert adr_files(ADR_DIR), "no records found"
    assert check_adr_directory(ADR_DIR) == []


def test_the_first_four_records_are_accepted_or_planned_as_the_plan_says() -> None:
    indexed = indexed_rows((ADR_DIR / "README.md").read_text(encoding="utf-8"))
    assert indexed["0001-reference-profile.md"] == "Accepted"
    assert indexed["0002-execution-ownership.md"] == "Accepted"
    assert indexed["0004-autonomy-levels.md"] == "Accepted"


def test_adr_0002_covers_every_protected_path() -> None:
    ids = inventory_row_ids(INVENTORY.read_text(encoding="utf-8"))
    assert len(ids) >= 100, "the inventory has lost its rows"
    adr = (ADR_DIR / "0002-execution-ownership.md").read_text(encoding="utf-8")
    assert uncovered(adr, ids) == []
    assert len(ownership_table_rows(adr)) == len(set(ownership_table_rows(adr))), (
        "a row is owned twice"
    )


def test_adr_0001_names_the_profile_switch_and_the_refusal_code() -> None:
    text = (ADR_DIR / "0001-reference-profile.md").read_text(encoding="utf-8")
    for needed in (
        "CALIBER_PROFILE=reference",
        "unsupported_in_reference_profile",
        "PostgreSQL 16",
        "irreversible_write",
        "reversible_write",
        "`export`",
    ):
        assert needed in text, needed


def test_adr_0002_names_every_lifespan_loop() -> None:
    from caliber.observability.worker_inventory import discover_lifespan_workers

    adr = (ADR_DIR / "0002-execution-ownership.md").read_text(encoding="utf-8")
    missing = [cls for _name, cls in discover_lifespan_workers() if f"`{cls}`" not in adr]
    assert missing == [], f"background loops with no owner in ADR 0002: {missing}"


# ---------------------------------------------------------------- the checks fail when they should


GOOD = "# ADR 0009 — Example\n\n## Status\n\nAccepted, 2026-01-01.\n\n## Context\n\nc\n\n## Decision\n\nd\n\n## Consequences\n\ne\n"
INDEX = "| ADR | Title | Status |\n| --- | --- | --- |\n| [0009](0009-example.md) | Example | Accepted |\n"


def write_directory(tmp_path: Path, record: str = GOOD, index: str = INDEX) -> Path:
    (tmp_path / "README.md").write_text(index)
    (tmp_path / "0009-example.md").write_text(record)
    return tmp_path


def test_a_consistent_directory_passes(tmp_path: Path) -> None:
    assert check_adr_directory(write_directory(tmp_path)) == []


@pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
def test_a_missing_heading_fails(tmp_path: Path, heading: str) -> None:
    record = GOOD.replace(f"{heading}\n", "")
    problems = check_adr_directory(write_directory(tmp_path, record))
    assert f"0009-example.md: missing {[heading]}" in problems


def test_headings_out_of_order_fail(tmp_path: Path) -> None:
    record = (
        GOOD.replace("## Context", "## TEMP")
        .replace("## Decision", "## Context")
        .replace("## TEMP", "## Decision")
    )
    (problem,) = check_adr_directory(write_directory(tmp_path, record))
    assert "in that order" in problem


def test_an_unlisted_record_fails(tmp_path: Path) -> None:
    directory = write_directory(tmp_path, index="| ADR | Title | Status |\n| --- | --- | --- |\n")
    assert check_adr_directory(directory) == ["0009-example.md is not listed in README.md"]


def test_an_index_row_without_a_file_fails(tmp_path: Path) -> None:
    directory = write_directory(tmp_path)
    (directory / "0009-example.md").unlink()
    assert check_adr_directory(directory) == [
        "README.md links to 0009-example.md, which does not exist"
    ]


def test_a_status_that_disagrees_with_the_index_fails(tmp_path: Path) -> None:
    directory = write_directory(tmp_path, record=GOOD.replace("Accepted, 2026", "Proposed, 2026"))
    (problem,) = check_adr_directory(directory)
    assert "README.md says 'Accepted', the record says 'Proposed'" in problem


def test_an_unknown_status_fails(tmp_path: Path) -> None:
    directory = write_directory(tmp_path, record=GOOD.replace("Accepted, 2026", "Maybe, 2026"))
    (problem,) = check_adr_directory(directory)
    assert "does not open with one of" in problem


def test_a_directory_without_an_index_fails(tmp_path: Path) -> None:
    assert check_adr_directory(tmp_path) == [f"{tmp_path / 'README.md'} is missing"]


def test_a_protected_path_without_an_owner_fails() -> None:
    adr = "### Protected paths\n\n| row | e | o | b |\n| --- | --- | --- | --- |\n| EP-001 | x | y | z |\n\n## Consequences\n"
    assert uncovered(adr, ["EP-001", "EP-002"]) == ["EP-002"]
    assert ownership_table_rows(adr) == ["EP-001"]


def test_a_row_id_in_prose_does_not_count_as_owned() -> None:
    adr = "### Protected paths\n\nEP-002 is mentioned here only.\n\n| row | e | o | b |\n| --- | --- | --- | --- |\n| EP-001 | x | y | z |\n"
    assert uncovered(adr, ["EP-001", "EP-002"]) == ["EP-002"]
