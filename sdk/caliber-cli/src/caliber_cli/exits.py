"""Exit codes, and why there are more than two of them.

A CLI that only ever exits 0 or 1 forces every caller to parse its output to
learn what happened. That is fine for a human and wrong for the thing this tool
is for, which is a CI job or a deploy script.

The code that matters most is :data:`AWAITING_HUMAN`. CALIBER stops and asks
people things: a refinement job produces a candidate and waits, an Aria plan
pauses on a question, a release candidate sits unsigned. None of those is a
failure and none is a success, and collapsing them into either one produces a
specific bad outcome:

* as success, a pipeline proceeds as though a human approved something;
* as failure, a pipeline reports a broken build for a system working correctly.

So it is its own code, and a caller decides what it means for them.
"""

from __future__ import annotations

#: The command did what was asked.
OK = 0

#: The command failed: an API error, a missing resource, a refused permission.
FAILURE = 1

#: The invocation itself was wrong — unknown command, missing argument, bad
#: value. ``argparse`` uses 2 for this, and matching it keeps one meaning.
USAGE = 2

#: The work stopped because a person has to act. Not an error.
AWAITING_HUMAN = 3

#: A quality gate said no. Distinct from FAILURE because the command *worked*:
#: the evaluation ran and the answer was "do not ship". A caller that treated
#: this as a crash would retry it, which cannot help.
GATE_FAILED = 4

#: The wait deadline passed with the work still in progress. Distinct from
#: FAILURE because nothing is known to be wrong — the answer is "not yet".
TIMEOUT = 5

#: No usable credential. Split out from FAILURE because the fix is specific and
#: always the same: set CALIBER_TOKEN, or issue a token.
UNAUTHENTICATED = 6


__all__ = [
    "AWAITING_HUMAN",
    "FAILURE",
    "GATE_FAILED",
    "OK",
    "TIMEOUT",
    "UNAUTHENTICATED",
    "USAGE",
]
