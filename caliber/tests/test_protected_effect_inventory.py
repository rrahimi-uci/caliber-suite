"""The protected-effect inventory is complete, true, and shipped.

``docs/architecture/protected-effect-paths.md`` lists every code path that can change an external system.
Four properties keep it from rotting into a stale document:

* **Complete**: every source file that contains an outbound-effect primitive has a row, so adding a new
  call site without classifying it fails this test.
* **Function-granular**: every function that contains a primitive is pinned to the row that classifies it
  (``fixtures/protected_effect_primitives.json``), with the number of each primitive it holds. A new
  primitive in a file or function that already has a row changes a pin and fails until someone reviews the
  row and updates the pin on purpose; a row is a claim about a function, not about a whole file.
* **True**: every row names a file and function that exist, the counts in the document's summary match
  the table, and no row carries an undecided note.
* **Shipped**: the wheel carries the same table, and an unclassified tool is treated as the riskiest class.

The tests that read repository files skip when run from an sdist without the docs.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from caliber.effects import inventory
from caliber.effects.inventory import (
    COLUMNS,
    EFFECT_CLASSES,
    FAIL_CLOSED,
    INVENTORY_PATH,
    InventoryError,
    InventoryRow,
    effect_class,
    effect_class_for_registry_tool,
    load_inventory,
    parse_inventory,
    tool_disposition,
    tool_disposition_for_registry_tool,
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
    "optimize_prompts(",
    "register_prompt(",
    "datasets.create_dataset(",
    "merge_records(",
    "entry.load(",
    "self._redis.publish",
    "self._nc.publish",
    "subprocess.",
    "urlopen(",
    "generate_presigned_url(",  # mints a capability URL (EP-053)
    # Holes found in review: object copies, uploads, shell and async subprocess spawns, a urllib opener,
    # and a direct import of subprocess's spawn functions. ``send_message`` was considered and left out: in
    # this tree it is the Aria assistant service's own method (assistant/service.py), not a messaging call.
    "copy_object",
    "upload_file(",
    "os.system(",
    "create_subprocess",
    "opener.open(",
    "from subprocess import",
)


def files_with_effect_primitives(root: Path) -> dict[Path, list[str]]:
    """Every ``*.py`` under ``root`` containing an effect primitive in code, with the primitives found.

    Comments and docstrings are blanked first (the same rule the pins use): a docstring that mentions
    ``optimize_prompts(...)`` is prose, not a call.
    """
    found: dict[Path, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        code = code_only(text, ast.parse(text))
        hits = [primitive for primitive in EFFECT_PRIMITIVES if primitive in code]
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


# ---------------------------------------------------------------- function-granular pins

PINS_PATH = Path(__file__).with_name("fixtures") / "protected_effect_primitives.json"
#: The symbol of a primitive outside any function: an import, a decorator or a route registration.
MODULE_LEVEL = "<module>"
#: A pin whose function is not the row's own entry point says why in more than a word.
_MIN_VIA = 20

PinKey = tuple[str, str]


def _char_column(line: str, byte_column: int) -> int:
    """``ast`` columns are UTF-8 byte offsets; text is indexed by characters."""
    return len(line.encode("utf-8")[:byte_column].decode("utf-8", errors="ignore"))


def _blank(lines: list[str], start: tuple[int, int], end: tuple[int, int]) -> None:
    """Overwrite the text between two (1-based line, character column) points with spaces."""
    (first, first_column), (last, last_column) = start, end
    for number in range(first, last + 1):
        line = lines[number - 1]
        low = first_column if number == first else 0
        high = last_column if number == last else len(line)
        lines[number - 1] = line[:low] + " " * (high - low) + line[high:]


def code_only(text: str, tree: ast.Module) -> str:
    """``text`` with comments and docstrings blanked, so a pin counts calls and not prose about them."""
    lines = text.split("\n")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
            and first.end_col_offset is not None
        ):
            _blank(
                lines,
                (first.lineno, _char_column(lines[first.lineno - 1], first.col_offset)),
                (first.end_lineno, _char_column(lines[first.end_lineno - 1], first.end_col_offset)),
            )
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            _blank(lines, token.start, token.end)
    return "\n".join(lines)


def outer_symbols(tree: ast.Module) -> list[tuple[int, int, str]]:
    """``(first line, last line, symbol)`` for each outermost function, ``Class.method`` for methods.

    A function nested in another belongs to the outer one: the row names what a caller reaches.
    """
    spans: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, classes: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert child.end_lineno is not None
                spans.append((child.lineno, child.end_lineno, ".".join([*classes, child.name])))
            elif isinstance(child, ast.ClassDef):
                visit(child, [*classes, child.name])
            else:
                visit(child, classes)

    visit(tree, [])
    return spans


def scan_pins(root: Path, *, base: Path) -> dict[PinKey, dict[str, int]]:
    """``{(file, symbol): {primitive: count}}`` for every ``*.py`` under ``root``, files relative to ``base``."""
    found: dict[PinKey, dict[str, int]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
        code = code_only(text, tree)
        spans = outer_symbols(tree)
        counts: dict[str, Counter[str]] = {}
        for primitive in EFFECT_PRIMITIVES:
            at = code.find(primitive)
            while at != -1:
                line = code.count("\n", 0, at) + 1
                symbol = next(
                    (name for first, last, name in spans if first <= line <= last), MODULE_LEVEL
                )
                counts.setdefault(symbol, Counter())[primitive] += 1
                at = code.find(primitive, at + 1)
        for symbol, counter in counts.items():
            found[(path.relative_to(base).as_posix(), symbol)] = dict(counter)
    return found


def load_pins(path: Path = PINS_PATH) -> dict[PinKey, dict[str, Any]]:
    entries = json.loads(path.read_text(encoding="utf-8"))["pins"]
    return {(entry["file"], entry["symbol"]): entry for entry in entries}


def pin_problems(
    found: dict[PinKey, dict[str, int]], pins: dict[PinKey, dict[str, Any]]
) -> list[str]:
    """Where the code and the pins disagree: a new, changed or stale function."""
    problems: list[str] = []
    for key in sorted(found.keys() | pins.keys()):
        label = f"{key[0]}::{key[1]}"
        if key not in pins:
            problems.append(
                f"{label} holds {found[key]} and has no pin: classify it in "
                "docs/architecture/protected-effect-paths.md, then add a pin naming that row"
            )
        elif key not in found:
            problems.append(f"{label} is pinned but holds no primitive any more: remove the pin")
        elif found[key] != pins[key]["primitives"]:
            problems.append(
                f"{label} now holds {found[key]} but is pinned at {pins[key]['primitives']}: "
                f"review row {pins[key]['row']} against the change, then update the pin"
            )
    return problems


def _entry_symbol(row: InventoryRow) -> str | None:
    return row.entry_point.split("::", 1)[1] if "::" in row.entry_point else None


def row_problems(
    pins: dict[PinKey, dict[str, Any]], rows: list[InventoryRow] | tuple[InventoryRow, ...]
) -> list[str]:
    """Where a pin's row does not exist, does not cover the file, or is not the function's own row."""
    by_id = {row.id: row for row in rows}
    problems: list[str] = []
    for (file, symbol), pin in sorted(pins.items()):
        label = f"{file}::{symbol}"
        row = by_id.get(pin["row"])
        if row is None:
            problems.append(f"{label} is pinned to {pin['row']}, which is not in the inventory")
            continue
        if not (row.path == file or (row.path.endswith("/") and file.startswith(row.path))):
            problems.append(f"{label} is pinned to {row.id}, whose entry point is in {row.path}")
            continue
        entry = _entry_symbol(row)
        own = entry is None or symbol == entry or symbol.endswith(f".{entry}")
        if not own and len(pin.get("via", "")) < _MIN_VIA:
            problems.append(
                f"{label} is pinned to {row.id} ({entry}) with no 'via' saying why that row covers it"
            )
    return problems


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


def test_a_docstring_or_comment_naming_a_primitive_needs_no_row_but_a_call_does(
    tmp_path: Path,
) -> None:
    (tmp_path / "prose.py").write_text(
        '"""Calls mlflow.genai.optimize_prompts(...) elsewhere."""\n# requests.post(url)\n',
        encoding="utf-8",
    )
    assert_every_effect_file_is_listed(tmp_path, [], base=tmp_path)
    (tmp_path / "call.py").write_text("def f():\n    os.system('x')\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"call\.py \(os\.system\(\)"):
        assert_every_effect_file_is_listed(tmp_path, [], base=tmp_path)


@pytest.mark.parametrize(
    ("source", "primitive"),
    [
        ("def f():\n    s3.copy_object(Bucket=b)\n", "copy_object"),
        ("def f():\n    s3.upload_file(a, b, c)\n", "upload_file("),
        ("def f():\n    os.system('x')\n", "os.system("),
        ("async def f():\n    await asyncio.create_subprocess_exec('x')\n", "create_subprocess"),
        ("def f():\n    opener.open(req)\n", "opener.open("),
        ("def f():\n    from subprocess import run\n    run(['x'])\n", "from subprocess import"),
        ("def f():\n    mlflow.genai.optimize_prompts(a, b)\n", "optimize_prompts("),
    ],
)
def test_each_widened_primitive_is_found_in_the_function_that_holds_it(
    tmp_path: Path, source: str, primitive: str
) -> None:
    assert _scan(tmp_path, source) == {("m.py", "f"): {primitive: 1}}


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


# ---------------------------------------------------------------- pins: the real tree


@needs_repo
def test_every_function_with_an_effect_primitive_is_pinned_to_its_row() -> None:
    pins = load_pins()
    problems = [
        *pin_problems(scan_pins(SOURCE, base=REPO), pins),
        *row_problems(pins, load_inventory()),
    ]
    assert not problems, (
        "caliber/tests/fixtures/protected_effect_primitives.json and the code disagree "
        "(a new primitive in an existing function must be reviewed against its row):\n"
        + "\n".join(problems)
    )


# ---------------------------------------------------------------- pins: the guard fails when it should


def _scan(tmp_path: Path, source: str) -> dict[PinKey, dict[str, int]]:
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    return scan_pins(tmp_path, base=tmp_path)


def _pin(primitives: dict[str, int], *, row: str = "EP-001", via: str = "") -> dict[str, Any]:
    return {"row": row, "primitives": primitives, "via": via}


_ONE_POST = "import httpx\n\ndef send():\n    httpx.post('u')\n"


def test_a_primitive_in_a_function_is_counted_against_that_function(tmp_path: Path) -> None:
    found = _scan(tmp_path, _ONE_POST)
    assert found == {("m.py", "send"): {"httpx.post": 1}}
    assert pin_problems(found, {("m.py", "send"): _pin({"httpx.post": 1})}) == []


def test_a_second_primitive_in_a_pinned_function_fails(tmp_path: Path) -> None:
    """The case a per-file scan misses: the file and the function already have a row."""
    pins = {("m.py", "send"): _pin({"httpx.post": 1})}
    again = pin_problems(_scan(tmp_path, _ONE_POST + "    httpx.post('v')\n"), pins)
    assert len(again) == 1
    assert "m.py::send now holds {'httpx.post': 2}" in again[0]
    assert "pinned at {'httpx.post': 1}" in again[0]
    assert "EP-001" in again[0]
    other = pin_problems(_scan(tmp_path, _ONE_POST + "    requests.delete('v')\n"), pins)
    assert len(other) == 1
    assert "requests.delete" in other[0]


def test_a_new_function_in_a_pinned_file_fails(tmp_path: Path) -> None:
    pins = {("m.py", "send"): _pin({"httpx.post": 1})}
    problems = pin_problems(
        _scan(tmp_path, _ONE_POST + "\ndef leak():\n    httpx.put('u')\n"), pins
    )
    assert len(problems) == 1
    assert "m.py::leak holds {'httpx.put': 1} and has no pin" in problems[0]


def test_a_removed_primitive_leaves_a_stale_pin(tmp_path: Path) -> None:
    pins = {("m.py", "send"): _pin({"httpx.post": 1})}
    problems = pin_problems(_scan(tmp_path, "def send():\n    return 1\n"), pins)
    assert problems == ["m.py::send is pinned but holds no primitive any more: remove the pin"]


def test_a_method_is_qualified_and_a_nested_function_belongs_to_the_outer_one(
    tmp_path: Path,
) -> None:
    found = _scan(
        tmp_path,
        "class Sender:\n"
        "    def go(self):\n"
        "        def inner():\n"
        "            httpx.post('u')\n"
        "        inner()\n"
        "\n"
        "    async def later(self):\n"
        "        requests.put('u')\n"
        "\n"
        "def plain():\n"
        "    return 1\n",
    )
    assert found == {
        ("m.py", "Sender.go"): {"httpx.post": 1},
        ("m.py", "Sender.later"): {"requests.put": 1},
    }


def test_a_primitive_outside_any_function_is_module_level(tmp_path: Path) -> None:
    found = _scan(tmp_path, "from gateway import invoke_tool\n\ndef quiet():\n    return 1\n")
    assert found == {("m.py", MODULE_LEVEL): {"invoke_tool": 1}}


def test_comments_and_docstrings_are_not_counted_but_code_is(tmp_path: Path) -> None:
    found = _scan(
        tmp_path,
        '"""Module note: httpx.post is forbidden."""\n'
        "\n"
        "# requests.post(url) is not called here\n"
        "def f():\n"
        '    """Never httpx.delete(x), and urlopen( is out."""  # httpx.put\n'
        "    requests.put(u)  # not httpx.post\n",
    )
    assert found == {("m.py", "f"): {"requests.put": 1}}


def test_a_call_split_over_lines_is_counted_and_its_docstring_mention_is_not(
    tmp_path: Path,
) -> None:
    found = _scan(
        tmp_path,
        "def tune():\n"
        '    """Calls optimize_prompts(\n'
        '    with a model."""\n'
        "    mlflow.genai.optimize_prompts(\n"
        "        x,\n"
        "    )\n",
    )
    assert found == {("m.py", "tune"): {"optimize_prompts(": 1}}


def test_ast_byte_columns_are_converted_to_character_columns() -> None:
    assert _char_column("é = 1", 2) == 1
    assert _char_column("abc", 2) == 2


def test_a_docstring_after_non_ascii_text_is_blanked_at_the_right_place(tmp_path: Path) -> None:
    found = _scan(
        tmp_path,
        'class K:\n    """é é é é"""\n\n    def f(self):\n        """é httpx.post(x) é"""\n'
        "        requests.delete(u)\n",
    )
    assert found == {("m.py", "K.f"): {"requests.delete": 1}}


def test_a_pin_must_name_a_real_row_that_covers_its_file() -> None:
    rows = [_row("EP-001", "pkg/a.py::Sender.go"), _row("EP-002", "tree/::upload")]
    pins = {
        ("pkg/a.py", "Sender.go"): _pin({"httpx.post": 1}, row="EP-001"),
        ("pkg/b.py", "go"): _pin({"httpx.post": 1}, row="EP-001"),
        ("pkg/a.py", "ghost"): _pin({"httpx.post": 1}, row="EP-999"),
        ("tree/deep/x.py", "helper"): _pin({"httpx.post": 1}, row="EP-002", via="why " * 6),
    }
    assert row_problems(pins, rows) == [
        "pkg/a.py::ghost is pinned to EP-999, which is not in the inventory",
        "pkg/b.py::go is pinned to EP-001, whose entry point is in pkg/a.py",
    ]


def test_a_pin_off_its_rows_function_needs_a_reason() -> None:
    rows = [_row("EP-001", "pkg/a.py::Sender.go"), _row("EP-002", "pkg/b.py::run")]
    helper = ("pkg/a.py", "Sender.helper")
    assert row_problems({helper: _pin({"httpx.post": 1})}, rows) == [
        "pkg/a.py::Sender.helper is pinned to EP-001 (Sender.go) with no 'via' saying why that "
        "row covers it"
    ]
    assert row_problems({helper: _pin({"httpx.post": 1}, via="too short")}, rows) != []
    assert row_problems({helper: _pin({"httpx.post": 1}, via="the helper of go " * 2)}, rows) == []
    # A row that names a method without its class matches that method in any class, and a row with
    # no ``::`` names the whole file.
    assert (
        row_problems({("pkg/b.py", "Worker.run"): _pin({"httpx.post": 1}, row="EP-002")}, rows)
        == []
    )
    whole = [_row("EP-003", "pkg/c.py")]
    assert (
        row_problems({("pkg/c.py", "anything"): _pin({"httpx.post": 1}, row="EP-003")}, whole) == []
    )


def test_the_shipped_pins_load_and_every_pin_counts_something() -> None:
    pins = load_pins()
    assert len(pins) >= 40
    for key, pin in pins.items():
        assert pin["primitives"], key
        assert all(count >= 1 for count in pin["primitives"].values()), key


# ---------------------------------------------------------------- the decisions the table records

#: The reference profile refuses these until they are explicitly supported (#641): host-file reads that
#: export unconfined host data, and database writes through the first-party MCP server.
HOST_FILE_TOOLS = ("read_text_file", "list_folder_files", "grep_files", "regex_search")
#: Registered by the DB MCP server's modes but only ever run in a database-enforced read-only transaction.
DB_MCP_READ_TOOLS = ("list_tables", "describe_table", "run_query", "similarity_search")


def db_mcp_write_tools() -> set[str]:
    """Names of the tools the first-party DB MCP server registers that do more than a read-only query.

    Derived from the source, not copied: a tool is a read tool only when every ``conn.*`` call it makes
    is ``conn.read_only_query``.
    """
    package = SOURCE / "mcp_servers" / "db"
    modules = {
        "rel": "tools_relational.py",
        "vec": "tools_vector.py",
        "graph": "tools_graph.py",
    }
    server = ast.parse((package / "server.py").read_text(encoding="utf-8"))
    registered: set[tuple[str, str]] = set()
    for node in ast.walk(server):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_TOOLS_BY_MODE"
            and node.value is not None
        ):
            registered = {
                (attribute.value.id, attribute.attr)
                for attribute in ast.walk(node.value)
                if isinstance(attribute, ast.Attribute)
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id in modules
            }
    writes: set[str] = set()
    for alias, name in registered:
        tree = ast.parse((package / modules[alias]).read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )
        calls = {
            call.func.attr
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "conn"
        }
        if calls - {"read_only_query"}:
            writes.add(name)
    return writes


@needs_repo
def test_the_db_mcp_scan_finds_the_write_tools_and_not_the_read_tools() -> None:
    writes = db_mcp_write_tools()
    assert {
        "create_table",
        "insert_rows",
        "execute_sql",
        "cypher_query",
        "upsert_vectors",
    } <= writes
    assert writes.isdisjoint(DB_MCP_READ_TOOLS)


def test_ep_055_and_ep_114_are_refused_in_the_reference_profile() -> None:
    by_id = {row.id: row for row in load_inventory()}
    for row_id in ("EP-055", "EP-114"):
        assert by_id[row_id].disposition == "refuse", row_id
    assert set(by_id["EP-055"].tool_names) == set(HOST_FILE_TOOLS)


@needs_repo
def test_every_db_mcp_write_tool_is_listed_on_ep_114_and_refused() -> None:
    """A new write tool in the DB MCP server fails here until someone classifies it."""
    writes = db_mcp_write_tools()
    (row,) = [row for row in load_inventory() if row.id == "EP-114"]
    assert set(row.tool_names) == writes
    for name in sorted(writes):
        assert tool_disposition(name, caliber_defined=True) == "refuse", name
        assert effect_class(name, caliber_defined=True) == "irreversible_write", name


@pytest.mark.parametrize("name", DB_MCP_READ_TOOLS)
def test_the_db_mcp_read_tools_are_not_refused_but_are_not_waved_through(name: str) -> None:
    assert tool_disposition(name, caliber_defined=True) == "dispatch"
    assert effect_class(name, caliber_defined=True) == FAIL_CLOSED


@pytest.mark.parametrize("name", HOST_FILE_TOOLS)
def test_the_host_file_tools_are_refused_and_stay_rated_export(name: str) -> None:
    assert tool_disposition(name, caliber_defined=True) == "refuse"
    assert effect_class(name, caliber_defined=True) == "export"


def test_a_tool_that_is_not_calibers_is_never_matched_by_name_to_a_refusal() -> None:
    """A registered tool called ``read_text_file`` is routed (fail-closed), neither refused nor trusted."""
    for name in (*HOST_FILE_TOOLS, "insert_rows", "sandbox_python"):
        assert tool_disposition(name) == "dispatch", name
        assert tool_disposition(name, caliber_defined=False) == "dispatch", name
        assert effect_class(name) == FAIL_CLOSED, name


def test_every_seeded_builtin_tool_is_routed_or_refused_as_decided() -> None:
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_NAMES

    refused = {"sandbox_python", *HOST_FILE_TOOLS}
    assert refused <= BUILTIN_TOOL_NAMES
    for name in sorted(BUILTIN_TOOL_NAMES):
        expected = "refuse" if name in refused else "dispatch"
        assert tool_disposition(name, caliber_defined=True) == expected, name


def test_an_unlisted_trusted_tool_is_routed_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_rows(
        monkeypatch,
        _row("EP-001", "a.py::f", tools=("shared",), disposition="dispatch"),
        _row("EP-002", "b.py::g", tools=("shared",), disposition="refuse"),
        _row("EP-003", "c.py::h", tools=("fine",), disposition="dispatch"),
    )
    assert tool_disposition("shared", caliber_defined=True) == "refuse", "refuse wins over dispatch"
    assert tool_disposition("fine", caliber_defined=True) == "dispatch"
    assert tool_disposition("never_listed", caliber_defined=True) == "dispatch"
    assert effect_class("never_listed", caliber_defined=True) == FAIL_CLOSED


# ---------------------------------------------------------------- trusted identity for effect_class


def _seed(name: str, **overrides: str) -> dict[str, str]:
    """Every field the seed writes for the built-in ``name``, with ``overrides`` applied."""
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE_BY_NAME, BUILTIN_TOOL_VERSION

    fields = {
        "owner": "@caliber",
        "module_path": BUILTIN_TOOL_MODULE_BY_NAME[name],
        "callable_name": name,
        "version": BUILTIN_TOOL_VERSION,
    }
    return {**fields, **overrides}


def test_a_registry_row_is_trusted_only_when_every_seeded_field_matches() -> None:
    assert effect_class_for_registry_tool(name="grep_files", **_seed("grep_files")) == "export"
    assert effect_class_for_registry_tool(
        name="write_workdir_file", **_seed("write_workdir_file")
    ) == ("reversible_write")
    assert effect_class_for_registry_tool(name="never_listed", **_seed("grep_files")) == FAIL_CLOSED
    for override in (
        {"owner": "@mallory"},
        {"owner": "@caliber", "module_path": "mallory.tools"},
        {"module_path": "caliber.workflows.memory_tools"},
        {"callable_name": "lookup_policy"},
        {"version": "2.0"},
        {"owner": "", "module_path": "", "callable_name": "", "version": ""},
    ):
        assert (
            effect_class_for_registry_tool(name="grep_files", **_seed("grep_files", **override))
            == FAIL_CLOSED
        ), override


def test_the_registry_disposition_trusts_only_a_built_in() -> None:
    assert tool_disposition_for_registry_tool(name="grep_files", **_seed("grep_files")) == "refuse"
    assert (
        tool_disposition_for_registry_tool(name="grok_parse", **_seed("grok_parse")) == "dispatch"
    )
    spoofed = _seed("grep_files", owner="@mallory")
    assert tool_disposition_for_registry_tool(name="grep_files", **spoofed) == "dispatch"
    assert effect_class_for_registry_tool(name="grep_files", **spoofed) == FAIL_CLOSED


def test_the_db_mcp_write_tools_are_not_refused_through_the_registry_helper() -> None:
    """Their trust comes from the MCP server's identity (#774/#759), not from a registry row: no registry row
    can be the first-party server's tool, so the registry helper never trusts these names."""
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE_BY_NAME

    assert "insert_rows" not in BUILTIN_TOOL_MODULE_BY_NAME
    row = {
        "owner": "@caliber",
        "module_path": "caliber.mcp_servers.db",
        "callable_name": "insert_rows",
    }
    assert (
        tool_disposition_for_registry_tool(name="insert_rows", version="1.0", **row) == "dispatch"
    )
    assert effect_class_for_registry_tool(name="insert_rows", version="1.0", **row) == FAIL_CLOSED


def test_every_seeded_registry_row_gets_its_inventory_class(
    db_session: Session,
) -> None:
    """The seed's own rows pass the trust check; a same-named row from anyone else does not."""
    from caliber.db.models import CaliberToolRegistry
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_NAMES, register_builtin_tools

    register_builtin_tools(db_session)
    rows = list(db_session.query(CaliberToolRegistry).all())
    assert {row.name for row in rows} == BUILTIN_TOOL_NAMES
    for row in rows:
        trusted = effect_class(row.name, caliber_defined=True)
        identity = {
            "owner": row.owner,
            "module_path": row.module_path,
            "callable_name": row.callable_name,
            "version": row.version,
        }
        assert effect_class_for_registry_tool(name=row.name, **identity) == trusted, row.name
        impostor = effect_class_for_registry_tool(
            name=row.name, **{**identity, "owner": "@someone"}
        )
        assert impostor == FAIL_CLOSED, row.name


def test_the_ir_and_the_resolver_entry_carry_no_owner() -> None:
    """Trust comes from the registry row read at dispatch, so the IR must not carry an owner to forge.

    The IR is written into generated exports field by field, so an owner there would be data an export
    author controls (and adding the field would change every export). If a later design needs one, it must
    also say how it stays out of exports and how the dispatcher re-reads the row; update the docs with it.
    """
    import dataclasses

    from caliber.workflows.ir import IRToolBinding
    from caliber.workflows.tools import ToolRegistryEntry

    for cls in (IRToolBinding, ToolRegistryEntry):
        assert "owner" not in {field.name for field in dataclasses.fields(cls)}, cls.__name__


#: The lookups that take a trust flag, and the helpers that derive it from a registry row's identity.
_TRUST_FLAG_LOOKUPS = {"effect_class", "tool_disposition"}
_REGISTRY_HELPERS = {"effect_class_for_registry_tool", "tool_disposition_for_registry_tool"}
#: The identity a registry helper must read from the row, never spell out.
_IDENTITY_FIELDS = {"name", "owner", "module_path", "callable_name", "version"}


def _called_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def literal_trust_calls(root: Path) -> list[str]:
    """Calls under ``root`` that hard-code trust in a tool instead of deriving it from its registry row.

    Flagged: a constant ``caliber_defined`` (or a second positional argument) to ``effect_class`` or
    ``tool_disposition``; a string constant for any identity field of the ``*_for_registry_tool`` helpers
    (``owner="@caliber"``); a ``**kwargs`` expansion into any of the four, which hides what is passed;
    and ``functools.partial`` of any of the four, which can pre-bind a constant out of sight.
    """
    found: list[tuple[str, int]] = []
    lookups = _TRUST_FLAG_LOOKUPS | _REGISTRY_HELPERS
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node.func)
            flagged = False
            if name == "partial" and node.args:
                flagged = _called_name(node.args[0]) in lookups
            elif name in lookups:
                flagged = any(keyword.arg is None for keyword in node.keywords)
                if name in _TRUST_FLAG_LOOKUPS:
                    flagged = flagged or len(node.args) > 1
                    flagged = flagged or any(
                        keyword.arg == "caliber_defined"
                        and isinstance(keyword.value, ast.Constant)
                        and bool(keyword.value.value)
                        for keyword in node.keywords
                    )
                else:
                    flagged = flagged or any(
                        keyword.arg in _IDENTITY_FIELDS
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                        for keyword in node.keywords
                    )
            if flagged:
                found.append((path.name, node.lineno))
    return [f"{file}:{line}" for file, line in sorted(found)]


@needs_repo
def test_no_production_call_hard_codes_that_a_tool_is_calibers_own() -> None:
    assert literal_trust_calls(SOURCE) == [], (
        "derive caliber_defined from the tool's registry identity "
        "(effects.inventory.effect_class_for_registry_tool), never pass True"
    )


def test_the_literal_trust_scan_sees_each_way_to_hard_code_it(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "effect_class('a', caliber_defined=True)\n"  # 1
        "tool_disposition('f', caliber_defined=1)\n"  # 2
        "inventory.effect_class('b', True)\n"  # 3
        "effect_class_for_registry_tool(name=n, owner='@caliber', module_path=m)\n"  # 4
        "tool_disposition_for_registry_tool(name=n, owner=o, module_path='caliber.x')\n"  # 5
        "effect_class_for_registry_tool(**identity)\n"  # 6
        "effect_class('c', **flags)\n"  # 7
        "functools.partial(effect_class, caliber_defined=True)('d')\n"  # 8
        "partial(tool_disposition_for_registry_tool, owner='@caliber')\n"  # 9
        "effect_class_for_registry_tool(name=n, owner=o, module_path=m, callable_name='x')\n",  # 10
        encoding="utf-8",
    )
    assert literal_trust_calls(tmp_path) == [f"bad.py:{n}" for n in range(1, 11)]


def test_the_literal_trust_scan_leaves_derived_trust_alone(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        "ok = effect_class('c', caliber_defined=trusted)\n"
        "ok2 = tool_disposition('g', caliber_defined=False)\n"
        "ok3 = effect_class('d')\n"
        "ok4 = effect_class_for_registry_tool(\n"
        "    name=row.name, owner=row.owner, module_path=row.module_path,\n"
        "    callable_name=row.callable_name, version=row.version)\n"
        "ok5 = tool_disposition_for_registry_tool(name=row.name, owner=row.owner, module_path=m,\n"
        "    callable_name=c, version=v)\n"
        "noise = len('e')\n"
        "other = functools.partial(print, 'x')\n",
        encoding="utf-8",
    )
    assert literal_trust_calls(tmp_path) == []


# ---------------------------------------------------------------- no undecided rows, no stale summary

_UNDECIDED = re.compile(r"\b(?:UNKNOWN|TBD|TODO|FIXME)\b|\bundecided\b|\bto be decided\b")


def undecided_rows(rows: list[InventoryRow] | tuple[InventoryRow, ...]) -> list[str]:
    return [row.id for row in rows if _UNDECIDED.search(row.notes)]


@needs_repo
def test_no_row_is_left_undecided() -> None:
    assert undecided_rows(load_inventory()) == [], (
        "decide the row (and say how) instead of flagging it"
    )


def test_an_undecided_note_is_found() -> None:
    rows = [
        _row("EP-001", "a.py::f"),
        InventoryRow("EP-002", "b.py::g", (), "read", "none", "exempt", "UNKNOWN whether vetted"),
        InventoryRow("EP-003", "c.py::h", (), "read", "none", "dispatch", "TBD by the owner"),
        InventoryRow("EP-004", "d.py::i", (), "read", "none", "dispatch", "an unknown MCP tool"),
    ]
    assert undecided_rows(rows) == ["EP-002", "EP-003"]


_SUMMARY = re.compile(
    r"(\d+) rows\. By current guard: ([^.]+)\. By disposition: ([^.]+)\. By effect class: ([^.]+)\."
)


def summary_problems(text: str, rows: list[InventoryRow] | tuple[InventoryRow, ...]) -> list[str]:
    """Where the document's own summary of the table disagrees with the table."""
    match = _SUMMARY.search(text)
    if match is None:
        return ["the document has no 'N rows. By current guard: ...' summary"]
    total, *breakdowns = match.groups()
    problems = (
        [] if int(total) == len(rows) else [f"summary says {total} rows, table has {len(rows)}"]
    )
    for label, text_counts, attribute in zip(
        ("current guard", "disposition", "effect class"),
        breakdowns,
        ("current_guard", "disposition", "effect_class"),
        strict=True,
    ):
        said = {
            name: int(number)
            for name, number in (part.strip().rsplit(" ", 1) for part in text_counts.split(","))
        }
        actual = dict(Counter(getattr(row, attribute) for row in rows))
        if said != actual:
            problems.append(f"summary by {label} says {said}, table has {actual}")
    return problems


@needs_repo
def test_the_summary_counts_match_the_table() -> None:
    assert summary_problems(DOCS_TABLE.read_text(encoding="utf-8"), load_inventory()) == []


def test_a_stale_summary_is_found() -> None:
    rows = [
        _row("EP-001", "a.py::f", klass="read", guard="none", disposition="dispatch"),
        _row("EP-002", "b.py::g", klass="export", guard="egress", disposition="refuse"),
    ]
    good = (
        "2 rows. By current guard: none 1, egress 1. By disposition: dispatch 1, refuse 1. "
        "By effect class: read 1, export 1."
    )
    assert summary_problems(good, rows) == []
    stale = good.replace("dispatch 1, refuse 1", "dispatch 2").replace("2 rows", "3 rows")
    assert summary_problems(stale, rows) == [
        "summary says 3 rows, table has 2",
        "summary by disposition says {'dispatch': 2}, table has {'dispatch': 1, 'refuse': 1}",
    ]
    assert summary_problems("no summary here", rows) == [
        "the document has no 'N rows. By current guard: ...' summary"
    ]


# ---------------------------------------------------------------- "no caller" claims stay true

#: Rows decided on the strength of "nothing calls this": the method each names, and its row.
NO_CALLER_CLAIMS = {
    "signed_url": "EP-053",
    "cleanup_run_files": "EP-050",
    "publish_status": "EP-090",
}


def callers_of(root: Path, names: set[str]) -> list[str]:
    """``file:line name`` for every call of one of ``names`` under ``root``, as a method or a function."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if called in names:
                found.append(f"{path.name}:{node.lineno} {called}")
    return found


@needs_repo
def test_the_rows_decided_because_nothing_calls_them_still_have_no_caller() -> None:
    by_id = {row.id: row for row in load_inventory()}
    for name, row_id in NO_CALLER_CLAIMS.items():
        assert by_id[row_id].entry_point.endswith(f".{name}"), f"{row_id} no longer names {name}"
    assert callers_of(SOURCE, set(NO_CALLER_CLAIMS)) == [], (
        "a caller appeared for a method whose row was decided because it had none: "
        "re-read the row (EP-050, EP-053, EP-090) and update its disposition and notes"
    )


def test_the_caller_scan_sees_methods_and_functions_and_ignores_definitions(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.py").write_text(
        "def signed_url():\n    return 1\n\nstore.signed_url('k')\nsigned_url()\nother()\n",
        encoding="utf-8",
    )
    assert callers_of(tmp_path, {"signed_url"}) == ["m.py:4 signed_url", "m.py:5 signed_url"]
