"""One dispatch attempt in its own process, so a scenario can kill it at an exact moment (B05.5b, #742).

``python -m tests.egi_scenarios.harness.effect_child`` does what the dispatcher (B04.1, #724) will do for one approved
proposal, with the platform's own modules: acquire the execution-owner lease, record ``dispatched`` under it, send the effect
through the real OpenAPI executor with the proposal's logical key as the idempotency key, and then *stop and wait to be killed*
at the moment the scenario names:

* ``--stop-at before_send``: the attempt is recorded as dispatched and nothing has been sent;
* ``--stop-at after_response``: the downstream answered and the attempt has not recorded the outcome.

(Killing it while a delayed answer is in flight, with ``--stop-at after_response`` and a downstream armed to answer late, is the
"after the downstream committed" case.) Progress is written to ``--marker`` so the test knows when it is safe to kill. It never
settles anything itself: the point is that nobody did.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from caliber.effects.connectors.document_request import submit_config, submit_input
from caliber.effects.records import DISPATCHED, proposal_state, proposal_view, transition
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import _send_raw, bind_egress_policy
from caliber.ownership import acquire

LEASE_SECONDS = 600.0
FOREVER = 3600.0


def _mark(path: Path, text: str) -> None:
    path.write_text(text)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.environ.get("EFFECT_CHILD_DB_URL"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--document-type", required=True)
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--stop-at", choices=("before_send", "after_response"), required=True)
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.db_url:
        parser.error("--db-url or EFFECT_CHILD_DB_URL is required")
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    engine = create_engine(args.db_url)
    with Session(engine) as session:
        lease = acquire(session, args.owner_id, ttl_seconds=LEASE_SECONDS)
        if lease is None:
            parser.error("the execution-owner lease is held")
        state = proposal_state(session, args.proposal_id)
        transition(
            session,
            args.proposal_id,
            DISPATCHED,
            expected_version=state.version,
            owner_id=args.owner_id,
            attempt=1,
            lease=lease,
        )
        key = proposal_view(session, args.proposal_id).logical_key
        session.commit()
    _mark(args.marker, "dispatched")
    if args.stop_at == "before_send":
        time.sleep(FOREVER)
    _send_raw(
        execution_config=submit_config(args.base_url, timeout_seconds=60.0),
        input_schema=None,
        input_data=submit_input(
            case_id=args.case_id,
            idempotency_key=key,
            expected_resource_version=args.expected_version,
            body={"document_type": args.document_type},
        ),
    )
    _mark(args.marker, "responded")
    time.sleep(FOREVER)


if __name__ == "__main__":  # pragma: no cover - the entry point of the child process
    main()
