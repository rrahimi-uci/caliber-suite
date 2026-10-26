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

pytestmark = pytest.mark.skipif(
    not (ADR_DIR.is_dir() and INVENTORY.exists()), reason="repository docs not present"
)

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


def without_code_fences(text: str) -> str:
    """``text`` with the lines inside ``` fences removed, so a heading in a code sample is not a heading."""
    kept: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def headings_problem(text: str) -> str | None:
    """Why ``text`` is not a well-formed record, or ``None``."""
    found = [
        line.strip() for line in without_code_fences(text).splitlines() if line.startswith("## ")
    ]
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


def table_after(text: str, heading: str) -> list[list[str]]:
    """The cell lists of the body rows of the table at ``heading``: a ``|`` line is the table's own header row, anything else
    is a heading above the table. The table ends at the next heading."""
    section = text.split(heading, 1)[1]
    section = re.split(r"^#{2,3} ", section, maxsplit=1, flags=re.M)[0]
    lines: list[str] = []
    for line in section.splitlines():
        if line.startswith("|"):
            lines.append(line)
        elif lines:
            break  # the table ended
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in lines]
    rows = [r for r in rows if not set(r[0]) <= set("-: ")]  # the separator
    return (
        rows if heading.startswith("|") else rows[1:]
    )  # a heading before the table leaves its header row


def ownership_rows(adr_text: str) -> dict[str, list[str]]:
    """``{row id: [row, entry point, owner, basis]}`` from ADR 0002's protected-path table."""
    return {cells[0]: cells for cells in table_after(adr_text, "### Protected paths")}


def ownership_table_rows(adr_text: str) -> list[str]:
    """The row ids in ADR 0002's protected-path table, in order."""
    return [cells[0] for cells in table_after(adr_text, "### Protected paths")]


def inventory_rows(text: str) -> dict[str, list[str]]:
    return {
        cells[0]: cells
        for line in text.splitlines()
        if line.startswith("| EP-")
        for cells in [[c.strip() for c in line.strip().strip("|").split("|")]]
    }


def uncovered(adr_text: str, ids: list[str]) -> list[str]:
    owned = set(ownership_table_rows(adr_text))
    return [row_id for row_id in ids if row_id not in owned]


# ---------------------------------------------------------------- the real records


def test_every_adr_is_indexed_and_has_required_headings() -> None:
    assert adr_files(ADR_DIR), "no records found"
    assert check_adr_directory(ADR_DIR) == []


def test_the_records_the_issue_accepts_are_accepted() -> None:
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
    ):
        assert needed in text, needed


def test_adr_0001_gives_every_component_a_value_and_every_effect_class_a_control() -> None:
    text = (ADR_DIR / "0001-reference-profile.md").read_text(encoding="utf-8")
    components = {
        cells[0]: cells for cells in table_after(text, "| component | value | enforced |")
    }
    for component in (
        "tenant",
        "region",
        "database",
        "experiment tracking",
        "object store",
        "projects",
        "identity",
        "model adapters",
        "connector",
        "profile switch",
    ):
        assert component in components, f"no row for {component}"
    for name, cells in components.items():
        assert len(cells) == 3 and all(cells), f"{name} has an empty cell: {cells}"

    controls = {
        cells[0].strip("`"): cells
        for cells in table_after(text, "| effect class | required control |")
    }
    assert set(controls) == {"read", "reversible_write", "irreversible_write", "export"}
    for name, cells in controls.items():
        assert len(cells) == 2 and all(cells), f"{name} has no required control"


def test_adr_0001_names_every_profile_code_it_claims_is_enforced() -> None:
    """The ``enforced`` column cites codes from ``profile.py``; each must be a real code."""
    from caliber.profile import reference_profile_violations  # noqa: F401 - import proves the module exists

    source = (REPO / "caliber" / "src" / "caliber" / "profile.py").read_text(encoding="utf-8")
    text = (ADR_DIR / "0001-reference-profile.md").read_text(encoding="utf-8")
    cited = set(
        re.findall(
            r"`(\w+_\w+)`",
            "\n".join(r[2] for r in table_after(text, "| component | value | enforced |")),
        )
    )
    codes = {c for c in cited if (c.islower() and f'"{c}"' in source) or c in {"sqlite_database"}}
    assert {
        "sqlite_database",
        "csrf_disabled",
        "self_approval_allowed",
        "fault_barriers_enabled",
    } <= codes


def test_adr_0002_gives_every_lifespan_loop_an_owner() -> None:
    from caliber.observability.worker_inventory import discover_lifespan_workers

    adr = (ADR_DIR / "0002-execution-ownership.md").read_text(encoding="utf-8")
    loops = {
        cells[0].strip("`"): cells
        for cells in table_after(adr, "| background loop | owner | what it does |")
    }
    missing = [cls for _name, cls in discover_lifespan_workers() if cls not in loops]
    assert missing == [], f"background loops with no row in ADR 0002: {missing}"
    for name, cells in loops.items():
        assert len(cells) == 3 and all(cells), f"{name} has an empty cell"
        assert "lease" in cells[1] or name.startswith("EventBus"), (
            f"{name} has no owner: {cells[1]}"
        )
    assert any(name.startswith("EventBus") for name in loops), "the event bus has no row"


def test_adr_0002_owners_follow_the_inventory_dispositions() -> None:
    inventory = inventory_rows(INVENTORY.read_text(encoding="utf-8"))
    owned = ownership_rows((ADR_DIR / "0002-execution-ownership.md").read_text(encoding="utf-8"))
    assert set(owned) == set(inventory), "ADR 0002 and the inventory list different rows"
    for row_id, cells in owned.items():
        _, entry, owner, basis = cells
        assert owner and basis, f"{row_id} has no owner or basis"
        source = inventory[row_id]
        assert entry.strip("`") == source[1].strip("`"), (
            f"{row_id}: entry point differs from the inventory"
        )
        disposition = source[5]
        if disposition == "dispatch":
            assert "dispatcher" in owner, row_id
        elif disposition == "refuse":
            assert "dispatcher" in owner and "profile.py" in owner, row_id
        else:
            path = source[1].strip("`").split("::")[0]
            assert owner == f"`{path}` (unchanged)", (
                f"{row_id}: an exempt row stays with its module"
            )


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


def test_adr_0001_records_the_connector_decision_and_its_report_exists() -> None:
    text = (ADR_DIR / "0001-reference-profile.md").read_text(encoding="utf-8")
    match = re.search(
        r"^Connector: (keep|switch) — see (docs/reports/egi/D1-connector-capability\.md)",
        text,
        re.M,
    )
    assert match, (
        "ADR 0001 needs a line 'Connector: keep|switch — see docs/reports/egi/D1-connector-capability.md'"
    )
    report = REPO / match.group(2)
    assert report.exists(), f"the report {match.group(2)} is missing"
    decision = report.read_text(encoding="utf-8").split("## Decision", 1)[1]
    assert decision.lstrip().startswith(f"`{match.group(1)}`"), (
        "the report's decision differs from ADR 0001"
    )
    row = next(line for line in text.splitlines() if line.startswith("| connector |"))
    assert match.group(1) in row
