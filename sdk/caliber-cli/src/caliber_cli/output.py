"""Two output modes, and a rule about which stream carries what.

``--json`` exists because parsing a table is how brittle automation gets built.
Every command supports it, and in JSON mode **stdout carries only JSON** — no
banners, no progress notes, no "waiting..." lines. Anything a human would want
to read goes to stderr, so ``caliberctl ... --json | jq`` works without a filter
step.

The table mode is for a person at a terminal, so it prints what a person needs
to act: the identifier they will paste into the next command, the state, and the
reason when there is one.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any


class Printer:
    """Renders results for either a human or a program.

    Holds the mode rather than threading a boolean through every command, and
    owns the stream discipline so no individual command can accidentally put a
    progress note on stdout in JSON mode.
    """

    def __init__(self, *, as_json: bool, quiet: bool = False) -> None:
        self.as_json = as_json
        self.quiet = quiet

    # -- human-facing notes ------------------------------------------------

    def note(self, message: str) -> None:
        """Progress and context. Always stderr; suppressed by ``--quiet``.

        On stderr even in table mode, because a human running the command still
        wants ``caliberctl workflow run ... > result.txt`` to capture the result
        and not the chatter.
        """
        if not self.quiet:
            print(message, file=sys.stderr)

    def warn(self, message: str) -> None:
        """Something the caller should know that did not stop the command."""
        print(f"warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)

    # -- results -----------------------------------------------------------

    def data(self, payload: Any) -> None:
        """One result object."""
        if self.as_json:
            self._json(payload)
            return
        for line in _describe(payload):
            print(line)

    def table(self, rows: Sequence[Any], columns: Sequence[str]) -> None:
        """A list of results.

        An empty list prints a note rather than a bare header, because a table
        with no rows and a table that was never fetched look identical, and the
        difference matters when you are debugging why a pipeline found nothing.
        """
        if self.as_json:
            self._json([_plain(row) for row in rows])
            return
        if not rows:
            self.note("(no results)")
            return

        plain = [_plain(row) for row in rows]
        cells = [[_cell(item.get(column)) for column in columns] for item in plain]
        widths = [
            max(len(column), *(len(row[index]) for row in cells))
            for index, column in enumerate(columns)
        ]
        print("  ".join(column.upper().ljust(widths[i]) for i, column in enumerate(columns)))
        for row in cells:
            print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))

    def _json(self, payload: Any) -> None:
        # ``default=str`` so an unexpected type degrades to its string form
        # rather than aborting after the command already had its effect --
        # failing to print a successful mutation is worse than printing it
        # imprecisely.
        print(json.dumps(_plain(payload), indent=2, sort_keys=True, default=str))


def _plain(value: Any) -> Any:
    """Convert SDK dataclasses to plain JSON-ready structures.

    Recursive because models nest (``Capabilities.extensibility.optimizers``),
    and a shallow conversion would emit ``repr`` strings for the inner objects.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _cell(value: Any) -> str:
    if value is None:
        # Not "None" and not "": a dash reads as "not reported" at a glance,
        # which is what a null field means here.
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) or "-"
    return str(value)


def _describe(payload: Any) -> Iterable[str]:
    plain = _plain(payload)
    if not isinstance(plain, dict):
        return [_cell(plain)]
    # ``extra`` is the forward-compatibility bag. Printing it would show a
    # human the same field twice whenever the server adds one this build models.
    items = [(key, value) for key, value in plain.items() if key != "extra"]
    if not items:
        return ["(empty)"]
    width = max(len(key) for key, _ in items)
    return [f"{key.ljust(width)}  {_cell(value)}" for key, value in items]


__all__ = ["Printer"]
