"""DISPOSABLE (#626): dispatch one stored proposal from its own process, so a test can kill that process.

``python -m tests.egi_scenarios.prototype.dispatch_child --db-url URL --base-url URL --proposal-id ID`` opens the
spike tables, loads the proposal and calls ``dispatch``. Against a downstream armed to answer late it blocks in
the HTTP call after the downstream committed, which is the moment the test kills it.
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.egi_scenarios.prototype import dispatch_spike as spike


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--proposal-id", required=True)
    args = parser.parse_args(argv)
    engine = create_engine(args.db_url)
    with Session(engine) as session:
        spike.dispatch(session, spike.load_proposal(session, args.proposal_id), args.base_url)


if __name__ == "__main__":  # pragma: no cover - the entry point of the child process
    main()
