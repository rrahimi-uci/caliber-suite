"""The protected-effect inventory: every code path that can change an external system.

The table lives in ``protected-effect-paths.md`` next to this module. ``docs/architecture/protected-effect-paths.md``
is the same file for readers of the docs; ``tests/test_protected_effect_inventory.py`` asserts the two are
identical and that every source file that can reach the outside world has a row.

Later work reads it here: the dispatcher's path matrix is parametrized from the rows, and the agent step
asks :func:`effect_class` how risky a model-proposed tool is. An unknown tool is treated as the riskiest
class, so a tool nobody classified can never be waved through. A tool *name* is not an identity (a
registered tool can reuse a built-in's name), so the table is trusted for a name only when the caller says
the tool is one CALIBER ships.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

#: The copy that ships in the wheel; the docs file is a byte-for-byte duplicate.
INVENTORY_PATH = Path(__file__).with_name("protected-effect-paths.md")

COLUMNS = (
    "id",
    "entry point",
    "tool names",
    "effect class",
    "current guard",
    "D3 disposition",
    "notes",
)
#: From least to most dangerous; the order is what makes "strictest wins" well defined.
EFFECT_CLASSES = ("read", "reversible_write", "export", "irreversible_write")
GUARDS = ("egress", "ledger", "approval", "none")
#: ``dispatch``: route through the dispatcher. ``refuse``: unsupported in the reference profile.
#: ``exempt``: platform-internal bookkeeping that no model proposal or workflow effect node can reach.
DISPOSITIONS = ("dispatch", "refuse", "exempt")
#: What an unclassified tool is treated as.
FAIL_CLOSED = "irreversible_write"
#: A table needs a header, a separator and at least one row.
_MIN_TABLE_LINES = 3
#: An ``exempt`` row must say why in more than a word; this is the shortest note that can.
_MIN_EXEMPT_NOTE = 20


class InventoryError(ValueError):
    """The inventory table is malformed."""


@dataclass(frozen=True)
class InventoryRow:
    id: str
    entry_point: str
    tool_names: tuple[str, ...]
    effect_class: str
    current_guard: str
    disposition: str
    notes: str

    @property
    def path(self) -> str:
        """The file (or directory, when it ends in ``/``) part of the entry point."""
        return self.entry_point.split("::", 1)[0]


def parse_inventory(text: str) -> list[InventoryRow]:
    """Parse the one table in ``text``; raise :class:`InventoryError` on any irregularity."""
    table = _the_table(text)
    if len(table) < _MIN_TABLE_LINES:
        raise InventoryError("no inventory table found (need a header, a separator and a row)")
    header = _cells(table[0])
    if tuple(header) != COLUMNS:
        raise InventoryError(f"header is {header}, expected {list(COLUMNS)}")
    if any(set(cell) - set("-: ") or not cell for cell in _cells(table[1])):
        raise InventoryError("the line under the header is not a table separator")
    rows: list[InventoryRow] = []
    for number, line in enumerate(table[2:], start=1):
        cells = _cells(line)
        if len(cells) != len(COLUMNS):
            raise InventoryError(f"row {number} has {len(cells)} cells, expected {len(COLUMNS)}")
        empty = [name for name, cell in zip(COLUMNS, cells, strict=True) if not cell]
        if empty:
            raise InventoryError(f"row {number} has empty cells: {empty}")
        row_id, entry, tools, effect, guard, disposition, notes = cells
        names = () if tools == "-" else tuple(t.strip() for t in tools.split(","))
        if not all(names):
            raise InventoryError(f"{row_id} has an empty tool name in {tools!r}")
        if row_id != f"EP-{number:03d}":
            raise InventoryError(f"row {number} has id {row_id!r}, expected 'EP-{number:03d}'")
        _check_choice(row_id, "effect class", effect, EFFECT_CLASSES)
        _check_choice(row_id, "current guard", guard, GUARDS)
        _check_choice(row_id, "D3 disposition", disposition, DISPOSITIONS)
        if disposition == "exempt" and len(notes) < _MIN_EXEMPT_NOTE:
            raise InventoryError(f"{row_id} is exempt but its notes do not say why")
        rows.append(
            InventoryRow(
                id=row_id,
                entry_point=entry.strip("`"),
                tool_names=names,
                effect_class=effect,
                current_guard=guard,
                disposition=disposition,
                notes=notes,
            )
        )
    return rows


def _the_table(text: str) -> list[str]:
    """The lines of the single run of ``|`` lines in ``text``; a second table is an error."""
    runs: list[list[str]] = []
    in_table = False
    for line in text.splitlines():
        if line.strip().startswith("|"):
            if not in_table:
                runs.append([])
            runs[-1].append(line.strip())
            in_table = True
        else:
            in_table = False
    if len(runs) > 1:
        raise InventoryError(f"the file holds {len(runs)} tables, expected exactly one")
    return runs[0] if runs else []


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _check_choice(row_id: str, column: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise InventoryError(f"{row_id} has {column} {value!r}, expected one of {list(allowed)}")


@functools.lru_cache(maxsize=1)
def load_inventory() -> tuple[InventoryRow, ...]:
    """The shipped inventory, parsed once."""
    return tuple(parse_inventory(INVENTORY_PATH.read_text(encoding="utf-8")))


def effect_class(tool_name: str, *, caliber_defined: bool = False) -> str:
    """The effect class of ``tool_name``: the strictest class among rows that list it.

    ``caliber_defined`` must be true only when the caller has established that the tool is one CALIBER
    ships (a seeded built-in, see ``workflows.builtin_tools.is_builtin_tool``, or an Aria toolset tool).
    ``CaliberToolRegistry`` is unique on (name, version) and the assistant publisher registers whatever
    name a draft carries, so a custom tool can be called ``grep_files``; without the flag the table is not
    consulted and the answer is :data:`FAIL_CLOSED`. A name no row lists is :data:`FAIL_CLOSED` too, never
    ``read``. A table that cannot be loaded raises :class:`InventoryError` (or ``OSError``) rather than
    answering: callers must treat an exception as a refusal.
    """
    if not caliber_defined:
        return FAIL_CLOSED
    classes = [row.effect_class for row in load_inventory() if tool_name in row.tool_names]
    if not classes:
        return FAIL_CLOSED
    return max(classes, key=EFFECT_CLASSES.index)
