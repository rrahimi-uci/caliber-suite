"""The protected-effect inventory is complete, true, and shipped.

``docs/architecture/protected-effect-paths.md`` lists every code path that can change an external system.
Three properties keep it from rotting into a stale document:

* **Complete**: every source file that contains an outbound-effect primitive has a row, so adding a new
  call site without classifying it fails this test.
* **True**: every row names a file and function that exist.
* **Shipped**: the wheel carries the same table, and an unclassified tool is treated as the riskiest class.

The scan is per file, not per function: one row anywhere in a file satisfies it, so a new effect function
in a file that already has a row is not caught here. It is a floor, not a proof; review of a new effect path
must add its row. The tests that read repository files skip when run from an sdist without the docs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from caliber.effects import inventory
from caliber.effects.inventory import (
    COLUMNS,
    EFFECT_CLASSES,
    FAIL_CLOSED,
    INVENTORY_PATH,
    InventoryError,
    InventoryRow,
    effect_class,
    load_inventory,
    parse_inventory,
)

REPO = Path(__file__).resolve().parents[2]
DOCS_TABLE = REPO / "docs" / "architecture" / "protected-effect-paths.md"
SOURCE = REPO / "caliber" / "src" / "caliber"

needs_repo = pytest.mark.skipif(not DOCS_TABLE.exists(), reason="repository docs not present")

#: Substrings that mark a file as able to reach the outside world. A match is deliberately broad
#: (``client.delete`` also matches an MLflow client's ``delete_prompt``): a false positive costs one
#: row with an honest note, a false negative is an unguarded effect nobody listed.
EFFECT_PRIMITIVES = (
    "httpx.post",
    "httpx.put",
    "httpx.patch",
    "httpx.delete",
    "httpx.request",
    "client.post",
    "client.put",
    "client.patch",
    "client.delete",
    "requests.post",
    "requests.put",
    "requests.patch",
    "requests.delete",
    "invoke_tool",
    "_perform_guarded_effect",
    "put_object",
    "delete_object",
    # MLflow writes, plugin imports and broker publishes the first scan missed (found in review).
    "mlflow.log_feedback(",
    "mlflow.log_expectation(",
    "mlflow.genai.evaluate(",
    "optimize_prompts(\n",  # a call split over lines; a docstring mention is not a call
    "register_prompt(",
    "datasets.create_dataset(",
    "merge_records(",
    "entry.load(",
    "self._redis.publish",
    "self._nc.publish",
    "subprocess.",
    "urlopen(",
)


def files_with_effect_primitives(root: Path) -> dict[Path, list[str]]:
    """Every ``*.py`` under ``root`` containing an effect primitive, with the primitives found."""
    found: dict[Path, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [primitive for primitive in EFFECT_PRIMITIVES if primitive in text]
        if hits:
            found[path] = hits
    return found


def assert_every_effect_file_is_listed(
    root: Path, rows: list[InventoryRow] | tuple[InventoryRow, ...], *, base: Path
) -> None:
    """Fail, naming each file, when a file with an effect primitive has no inventory row."""
    listed = [row.path for row in rows]
    unlisted = []
    for path, hits in files_with_effect_primitives(root).items():
        relative = path.relative_to(base).as_posix()
        covered = any(
            relative == entry or (entry.endswith("/") and relative.startswith(entry))
            for entry in listed
        )
        if not covered:
            unlisted.append(f"{relative} ({', '.join(hits)})")
    assert not unlisted, (
        "source files can reach the outside world but have no row in "
        "docs/architecture/protected-effect-paths.md (and its packaged copy "
        "caliber/src/caliber/effects/protected-effect-paths.md): " + "; ".join(unlisted)
    )


# ---------------------------------------------------------------- the real inventory


@needs_repo
def test_inventory_rows_are_complete() -> None:
    rows = parse_inventory(DOCS_TABLE.read_text(encoding="utf-8"))
    assert len(rows) >= 20, (
        "the inventory covers the runtime, tools, MCP, OpenAPI, storage and registry paths"
    )
    assert [row.id for row in rows] == [f"EP-{n:03d}" for n in range(1, len(rows) + 1)]
    for row in rows:
        assert row.effect_class in EFFECT_CLASSES
        assert row.notes, f"{row.id} has no notes"


@needs_repo
def test_every_effect_primitive_file_is_listed() -> None:
    assert_every_effect_file_is_listed(SOURCE, load_inventory(), base=REPO)


@needs_repo
def test_every_row_names_a_real_file_and_function() -> None:
    for row in load_inventory():
        path = REPO / row.path
        assert path.exists(), f"{row.id}: {row.path} does not exist"
        if "::" in row.entry_point:
            name = row.entry_point.split("::", 1)[1]
            assert defines(path, name), f"{row.id}: {row.path} defines no {name!r}"
        else:
            assert row.path.endswith("/") or path.is_file(), f"{row.id}: {row.path} is not a file"


def defines(path: Path, dotted: str) -> bool:
    """Whether ``path`` defines function or class ``dotted`` (``name`` anywhere, or ``Class.method`` inside it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    owner, _, member = dotted.rpartition(".")
    if not owner:
        return any(isinstance(node, kinds) and node.name == member for node in ast.walk(tree))
    return any(
        isinstance(cls, ast.ClassDef)
        and cls.name == owner
        and any(isinstance(node, kinds) and node.name == member for node in ast.walk(cls))
        for cls in ast.walk(tree)
    )


def test_defines_ties_a_method_to_its_class_and_matches_whole_names(tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    source.write_text(
        "class FooBar:\n    def run(self): ...\n\nclass Other:\n    async def go(self): ...\n\ndef top(): ...\n"
    )
    assert defines(source, "top")
    assert defines(source, "FooBar.run")
    assert defines(source, "Other.go")
    assert not defines(source, "Foo"), "a prefix of a class name is not that class"
    assert not defines(source, "Other.run"), "run belongs to FooBar, not Other"
    assert not defines(source, "FooBar.go")
    assert not defines(source, "missing")


@needs_repo
def test_an_exempt_row_has_a_reason_and_no_tool_names() -> None:
    """Exempt means no model proposal can reach it, so a registered tool name there is a contradiction."""
    for row in load_inventory():
        if row.disposition == "exempt":
            assert len(row.notes) >= 20, row.id
            assert row.tool_names == (), f"{row.id} is exempt but lists tool names {row.tool_names}"


@needs_repo
def test_packaged_copy_matches_docs() -> None:
    assert INVENTORY_PATH.read_bytes() == DOCS_TABLE.read_bytes(), (
        "caliber/src/caliber/effects/protected-effect-paths.md must equal "
        "docs/architecture/protected-effect-paths.md: copy the docs file over the packaged one"
    )


# ---------------------------------------------------------------- the guard fails when it should


def test_unlisted_call_site_fails(tmp_path: Path) -> None:
    rogue = tmp_path / "rogue_effect.py"
    rogue.write_text(
        "import httpx\n\ndef leak():\n    httpx.post('https://example.test', json={})\n"
    )
    with pytest.raises(AssertionError) as failure:
        assert_every_effect_file_is_listed(tmp_path, load_inventory(), base=tmp_path)
    assert "rogue_effect.py" in str(failure.value)
    assert "httpx.post" in str(failure.value)


def test_a_listed_file_and_a_listed_directory_pass(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "inner.py").write_text("storage.put_object(Key='k')\n")
    (tmp_path / "single.py").write_text("requests.post(url)\n")
    (tmp_path / "quiet.py").write_text("print('no outbound call here')\n")
    rows = [
        _row("EP-001", "single.py::send"),
        _row("EP-002", "pkg/::upload"),
    ]
    assert_every_effect_file_is_listed(tmp_path, rows, base=tmp_path)


def test_a_new_primitive_in_a_listed_file_still_passes_but_a_new_file_does_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("httpx.put(url)\n")
    (tmp_path / "b.py").write_text("httpx.delete(url)\n")
    with pytest.raises(AssertionError, match=r"b\.py"):
        assert_every_effect_file_is_listed(tmp_path, [_row("EP-001", "a.py::go")], base=tmp_path)


# ---------------------------------------------------------------- lookup semantics


def _row(
    row_id: str,
    entry_point: str,
    *,
    tools: tuple[str, ...] = (),
    klass: str = "read",
    guard: str = "none",
    disposition: str = "dispatch",
) -> InventoryRow:
    return InventoryRow(row_id, entry_point, tools, klass, guard, disposition, "a note")


def _with_rows(monkeypatch: pytest.MonkeyPatch, *rows: InventoryRow) -> None:
    monkeypatch.setattr(inventory, "load_inventory", lambda: rows)


def test_effect_class_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_rows(
        monkeypatch,
        _row("EP-001", "a.py::f", tools=("lookup_case", "list_cases"), klass="read"),
        _row("EP-002", "b.py::g", tools=("document_request",), klass="irreversible_write"),
        _row("EP-003", "c.py::h", tools=("publish_prompt",), klass="reversible_write"),
        _row("EP-004", "d.py::i", tools=("export_bundle",), klass="export"),
    )
    assert effect_class("lookup_case", caliber_defined=True) == "read"
    assert effect_class("document_request", caliber_defined=True) == "irreversible_write"
    assert effect_class("publish_prompt", caliber_defined=True) == "reversible_write"
    assert effect_class("export_bundle", caliber_defined=True) == "export"


def test_unknown_tool_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_rows(monkeypatch, _row("EP-001", "a.py::f", tools=("lookup_case",), klass="read"))
    assert (
        effect_class("never_classified", caliber_defined=True)
        == FAIL_CLOSED
        == "irreversible_write"
    )
    assert effect_class("", caliber_defined=True) == FAIL_CLOSED
    assert effect_class("lookup", caliber_defined=True) == FAIL_CLOSED, (
        "a prefix of a listed name is not that name"
    )


def test_a_tool_listed_by_two_rows_gets_the_strictest_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_rows(
        monkeypatch,
        _row("EP-001", "a.py::f", tools=("shared",), klass="read"),
        _row("EP-002", "b.py::g", tools=("shared",), klass="export"),
        _row("EP-003", "c.py::h", tools=("shared",), klass="reversible_write"),
    )
    assert effect_class("shared", caliber_defined=True) == "export"


def test_a_custom_tool_with_a_builtin_name_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry is unique on (name, version), so a registered tool can be called ``grep_files``."""
    _with_rows(monkeypatch, _row("EP-001", "a.py::f", tools=("grep_files",), klass="read"))
    assert effect_class("grep_files", caliber_defined=True) == "read"
    assert effect_class("grep_files") == FAIL_CLOSED
    assert effect_class("grep_files", caliber_defined=False) == FAIL_CLOSED


#: The registry's own word for how dangerous a seeded tool is, and the least the inventory may say about it.
_AT_LEAST = {"read": "read", "write": "reversible_write", "external_action": "irreversible_write"}


@needs_repo
def test_every_seeded_builtin_tool_is_classified_at_least_as_strictly_as_the_registry_says() -> (
    None
):
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_NAMES, BUILTIN_TOOL_SIDE_EFFECTS

    listed = {name for row in load_inventory() for name in row.tool_names}
    missing = sorted(BUILTIN_TOOL_NAMES - listed)
    assert not missing, (
        f"built-in tools with no inventory row (they fall to the fail-closed class): {missing}"
    )
    for name, declared in BUILTIN_TOOL_SIDE_EFFECTS.items():
        floor = _AT_LEAST[declared]
        assert EFFECT_CLASSES.index(
            effect_class(name, caliber_defined=True)
        ) >= EFFECT_CLASSES.index(floor), name


@needs_repo
def test_load_inventory_parses_the_shipped_table_once() -> None:
    first = load_inventory()
    assert first == tuple(parse_inventory(INVENTORY_PATH.read_text(encoding="utf-8")))
    assert load_inventory() is first


# ---------------------------------------------------------------- the parser refuses a bad table

_HEADER = "| " + " | ".join(COLUMNS) + " |\n| " + " | ".join("---" for _ in COLUMNS) + " |\n"
_GOOD_ROW = "| EP-001 | `a.py::f` | t1, t2 | read | none | dispatch | because |"


def test_the_parser_reads_a_minimal_table() -> None:
    (row,) = parse_inventory(_HEADER + _GOOD_ROW)
    assert row.id == "EP-001"
    assert row.path == "a.py"
    assert row.entry_point == "a.py::f"
    assert row.tool_names == ("t1", "t2")
    assert (row.effect_class, row.current_guard, row.disposition) == ("read", "none", "dispatch")


def test_a_dash_in_tool_names_means_none() -> None:
    (row,) = parse_inventory(_HEADER + _GOOD_ROW.replace("t1, t2", "-"))
    assert row.tool_names == ()


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("no table at all", "no inventory table"),
        (_HEADER.replace("entry point", "entry") + _GOOD_ROW, "header is"),
        (_HEADER + _GOOD_ROW + " extra |", "cells"),
        (_HEADER + _GOOD_ROW.replace("| none |", "| |"), "empty cells"),
        (_HEADER + _GOOD_ROW.replace("| read |", "| mutate |"), "effect class"),
        (_HEADER + _GOOD_ROW.replace("| none |", "| firewall |"), "current guard"),
        (_HEADER + _GOOD_ROW.replace("| dispatch |", "| maybe |"), "D3 disposition"),
        (_HEADER + _GOOD_ROW.replace("EP-001", "EP-002"), "expected 'EP-001'"),
        (_HEADER + _GOOD_ROW.replace("| dispatch |", "| exempt |"), "do not say why"),
        (_HEADER + _GOOD_ROW + "\n\ntext\n\n" + _HEADER + _GOOD_ROW, "2 tables"),
        (_HEADER.replace("---", "abc") + _GOOD_ROW, "not a table separator"),
        (_HEADER + _GOOD_ROW.replace("t1, t2", "t1,,t2"), "empty tool name"),
    ],
)
def test_the_parser_refuses_a_malformed_table(table: str, message: str) -> None:
    with pytest.raises(InventoryError, match=message):
        parse_inventory(table)


def test_an_exempt_row_with_a_reason_is_accepted() -> None:
    row = _GOOD_ROW.replace(
        "| dispatch | because |",
        "| exempt | user upload authorized by project RBAC, not agent-reachable |",
    )
    (parsed,) = parse_inventory(_HEADER + row)
    assert parsed.disposition == "exempt"
