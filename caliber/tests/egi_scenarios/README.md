# EGI scenario tests

End-to-end scenarios for the EGI reference architecture (V01–V35 in the delivery plan). Each proves one
claim about the platform by acting on a real CALIBER app, a real downstream system of record and, when the
claim is about recovery, a real process that is killed on purpose.

## Convention

- One file per scenario: `test_vNN_<slug>.py` (for example `test_v07_ambiguous_timeout.py`), created by the
  issue that owns scenario `vNN`. `test_v00_harness_smoke.py` proves this harness and owns no claim.
- A scenario measures effects at the **downstream** (`downstream.mutations()`, `downstream.query(key)`),
  never in CALIBER's logs: a platform that lies about what it did must not be able to pass.
- A test needs no network, no credential and no live model. Providers are the offline ones and the model is
  `scripted_model`.
- Run from `caliber/`: `pytest tests/egi_scenarios -q`. PostgreSQL cases skip unless
  `CALIBER_TEST_POSTGRES_URL` is set (an admin connection, see `tests/dialect_helpers.py`); in CI they run in
  the required job "EGI scenarios (PostgreSQL)". Locally: `scripts/ci-local.sh egi-scenarios-postgres`.

## Fixtures (`conftest.py`)

| fixture | what you get |
| --- | --- |
| `downstream` | The disposable system of record (`downstream/app.py`) as a real process, reset to its seed before every test that uses it. `mutations()`, `query(key)`, `set_fault(...)`, `restart()`. |
| `reference_app` | A migrated CALIBER app on a fresh database, once per dialect. `.client` (Starlette `TestClient`), `.headers(user, project)` (acts as a user through the trusted proxy), `.env` (the environment that runs the same config in another process), `.session_factory`. On PostgreSQL it runs `CALIBER_PROFILE=reference`; the reference profile refuses SQLite, so the SQLite instance runs `development` with the same safe settings. |
| `two_workspaces` | Project A (`editor_a`, `approver_a`) and project B (`editor_b`), seeded. |
| `scripted_model` | The two model turns of the journey: `request_missing_document(case_id, document_type)`, then `completed`. |

## Killing a process at a named barrier

Production code calls `caliber.testing.fault_barriers.barrier(name)` at the points listed in
`BARRIERS` (`before_dispatch`, `after_downstream_commit`, `before_receipt_persist`, `after_outbox_commit`,
`after_evaluation_claim`). Unarmed it does nothing. A process started with `CALIBER_FAULT_BARRIERS=<name>`
exits with status 137 when it reaches that barrier, without cleanup, like a crash. The reference profile
refuses to start while the variable is set.

```python
with ServerProcess(
    {**reference_app.env, "CALIBER_FAULT_BARRIERS": "after_downstream_commit"}
) as server:
    ...  # drive the scenario to the barrier
    assert server.wait_for_exit() == 137
    server.restart()  # a fresh, unarmed process on the same port and database
    ...  # recover, then assert on what the downstream saw: downstream.mutations() == 1
```

`harness/process.py::ServerProcess` starts `uvicorn caliber.server:create_app --factory` configured by the
`env` it is given alone (no ambient `CALIBER_*` variable reaches it), waits for the health endpoint, and supports `kill()`, `restart()`, `wait_for_exit()`, `exit_code` and the context manager.
The platform's own barriers arrive with the dispatcher and the ledger (#522); until then
`harness/barrier_app.py` reaches a barrier on request so the harness itself is tested.

The dispatcher itself calls two of them, `before_dispatch` (the claim is committed, nothing is sent) and
`after_downstream_commit` (the downstream answered, nothing is recorded). `harness/worker_child.py` is one execution-owner
pass of the workflow-run worker in its own process (the lease, a scripted model, one tick), so
`test_b05_governed_worker_wiring.py` can kill a worker at either of them and show that the new owner never sends a second time.

`test_b05_http_unknown_settlement.py` (#1055) drives a webhook workflow through the same worker against a second downstream of its own (an in-process server that counts its commits, honours the key and answers the read-by-key contract of the generic `http` connector): a timeout after the send, a worker killed on either side of it and a lookup that never shows the request all end with one commit and no second send.
