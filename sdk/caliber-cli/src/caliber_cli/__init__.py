"""``caliberctl`` — non-interactive operator commands for CALIBER.

A thin wrapper over ``caliber-sdk``. It invents no backend semantics: where a
command appears to decide something, it is translating a state the server
reported into an exit code.

Exit codes are the interface, because a tool for CI jobs that only ever exits 0
or 1 forces its callers to parse output:

===  ===========================================================
0    the command did what was asked
1    failure — API error, missing resource, refused permission
2    the invocation was wrong, or a confirmation flag was missing
3    the work stopped because a person has to act (not an error)
4    a quality gate said no (the command worked; the answer was no)
5    the wait deadline passed with the work still in progress
6    no usable credential
===  ===========================================================
"""

from __future__ import annotations

from caliber_cli.cli import build_parser, main

__version__ = "0.1.0.dev0"

__all__ = ["__version__", "build_parser", "main"]
