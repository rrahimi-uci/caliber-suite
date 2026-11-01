"""One execution-owner pass of the workflow-run worker in its own process, so a scenario can kill it at a fault barrier (#1051).

``python -m tests.egi_scenarios.harness.worker_child --owner-id <id>`` is what the server's lifespan does for one tick, with the
platform's own modules: take the execution-owner lease, hand the worker a lease provider that reads it, and run one worker tick
(claim a queued run, execute it, dispatch what it is approved to dispatch). The configuration is the process environment alone
(``CaliberConfig.load()``), and the model is scripted (the same turn every time). If ``CALIBER_FAULT_BARRIERS`` names a barrier
the dispatcher reaches, the process exits with status 137 there, without cleanup, like a crash.
"""

from __future__ import annotations

import argparse

from caliber.config import CaliberConfig
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy
from caliber.orchestrator import workflow_run_worker as worker_module
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.ownership import acquire
from tests.egi_scenarios.test_b05_governed_worker_wiring import proposing_executor

LEASE_SECONDS = 600.0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-id", required=True)
    args = parser.parse_args(argv)
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    config = CaliberConfig.load()
    factory = sessionmaker_from_engine(create_engine_from_config(config))
    with factory() as session:
        lease = acquire(session, args.owner_id, ttl_seconds=LEASE_SECONDS)
        session.commit()
    if lease is None:
        parser.error("the execution-owner lease is held")
    worker_module.build_executor = lambda *_a, **_k: proposing_executor()  # type: ignore[assignment]
    WorkflowRunWorker(factory, config=config, lease_provider=lambda: lease)._tick()


if __name__ == "__main__":  # pragma: no cover - the entry point of the child process
    main()
