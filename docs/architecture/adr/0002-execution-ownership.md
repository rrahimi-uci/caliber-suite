# ADR 0002 — Execution ownership

## Status

Accepted, 2026-10-26. Cites ADR 0001 (the reference profile) and `docs/architecture/protected-effect-paths.md`
(the protected-effect inventory, #627).

## Context

Four things today have no single owner. (1) Control records the EGI program adds (run manifest, capability
envelope, policy decision, approval, effect record, receipt, acceptance decision, evidence package, outbox) have
no agreed writer: two modules could each persist "the" receipt. (2) Thirteen background workers and an event bus are
started by the server lifespan, and nothing says which process may run them when two processes share a database (a second
active owner doubles effects). (3) many code paths can change an external system (the inventory lists them all); some run through
the effect ledger, most run through nothing. (4) Packages B02–B06 each need to answer "which module writes this
record" before they can be built independently.

## Decision

**Every control record has exactly one writer module and every protected path has exactly one owner. A module that
is not the writer reads the record and never updates it.**

### Records

| record | writer module | reader modules |
| --- | --- | --- |
| RunManifest | `caliber/src/caliber/run_manifest.py` (#517) | the workflow runtime, the evidence package, the UI |
| CapabilityEnvelope, PolicyDecision | `caliber/src/caliber/policy/` (#518) | the dispatcher, the approval boundary, the evidence package |
| ApprovalRequest, ApprovalDecision | `caliber/src/caliber/workflows/approval_policy.py` and `caliber/src/caliber/routes/workflow_runs.py` (#523) | the dispatcher, the evidence package, the UI |
| EffectProposal, EffectRecord, Receipt | `caliber/src/caliber/effects/` (#520, #522): the dispatcher's `submit_proposal` writes the proposal | the workflow runtime, recovery, the evidence package |
| execution-owner lease and fencing token | `caliber/src/caliber/ownership.py` (#536) | every background worker, the dispatcher |
| AcceptanceDecision | `caliber/src/caliber/acceptance/` (#527) | release and run-completion logic, the evidence package |
| evidence package | `caliber/src/caliber/evidence/` (#538) | the UI, the export route, auditors |
| outbox, inbox | `caliber/src/caliber/events/` (#536) | the webhook dispatcher, the event buses, consumers |

### Background loops

Started in `caliber/src/caliber/server.py::create_app`'s lifespan. The thirteen workers are listed by
`caliber/src/caliber/observability/worker_inventory.py`; the event bus (`start_bus()`) is started in the same
lifespan but is invisible to that inventory, so it has its own row. **The decision:** in the reference profile
exactly one process holds the execution-owner lease at a time and only the holder runs the workers; the others
serve requests. The lease is `caliber/src/caliber/ownership.py` (#668) and the lifespan obeys it
(`caliber/src/caliber/execution_role.py`, #673); each worker still also claims individual jobs with its own lease
(`workflow_run_lease_seconds`, `knowledge_build_lease_seconds`, `workspace_import_lease_seconds`), and fencing every
transition with the token arrives with the outbox (#685). This record fixes who owns what.

| background loop | owner | what it does |
| --- | --- | --- |
| `RefinementWorker` | execution-owner lease (B13, #536) | Advances refinement jobs through the 6-stage pipeline. |
| `WorkflowRunWorker` | execution-owner lease (B13, #536) | Executes queued workflow runs and their step checkpoints. |
| `AriaPlanWorker` | execution-owner lease (B13, #536) | Resumes and advances durable Aria goal-plan execution. |
| `KnowledgeBaseWorker` | execution-owner lease (B13, #536) | Runs knowledge-base build/sync jobs and their events. |
| `WorkflowSchedulerTask` | execution-owner lease (B13, #536) | Sweeps cron-scheduled workflow deployments and enqueues runs. |
| `JanitorTask` | execution-owner lease (B13, #536) | Reclaims stuck refinement jobs and prunes retained workflow runs. |
| `ReleaseReconcilerTask` | execution-owner lease (B13, #536) | Settles incomplete prompt-alias release operations after missed webhooks. |
| `CalibrationDrain` | execution-owner lease (B13, #536) | Drains queued tool-calibration jobs against the live registry. |
| `WebhookDispatcher` | execution-owner lease (B13, #536) | Delivers accepted outbound webhook events; retries into the dead-letter queue. |
| `OutboxPublisher` | execution-owner lease (B13, #536) | Hands recorded business events (the transactional outbox) to each consumer's inbox, with retry and backoff; delivery is at-least-once and consumers deduplicate on the event id. |
| `WorkspaceImportWorker` | execution-owner lease (B13, #536) | Claims queued push-mode Workspace import jobs and materializes each into an immutable WorkspaceRevision. |
| `WorkspaceReleaseEvaluator` | execution-owner lease (B13, #536) | Claims and executes queued Workspace release evaluations from the release revision's immutable candidate, dataset and judge pins, oldest first and fenced by the lease; B10.3 persists per-example gate evidence, B10.4 records/reclaims expired attempts with durable history and stale-worker fencing, and B10.5 requires matching successful evidence before governed decisions or break-glass apply. |
| `EffectReconcilerTask` | execution-owner lease (B13, #536) | Calls the effect reconciler under the captured lease (B05.5d, #997): asks each connector bound in `CALIBER_EFFECT_CALLBACK_CONNECTORS` about `unknown` and abandoned effects by their logical key, and moves a dispatch stuck in flight under the *current* lease to `unknown`. Never re-sends an effect. After each pass it also finalizes the governed-step token reservations a crashed worker left open past their deadline (B08.4, #761), at their whole reservation: it never gives tokens back. |
| `EventBus` (database, Redis or NATS) | every process (fans events out; takes no effect) | publisher and poll tasks (database bus) or consumer tasks (Redis, NATS) deliver platform events to subscribers; publishing becomes idempotent through the outbox (#536) |

### Recovery by stage

What each loop leaves behind when its owner dies in the middle of a tick, who picks it up, and which table shows it. The
rows are the thirteen loops of `worker_inventory()` (the event bus is not a loop and has no row), and
`caliber/tests/test_worker_inventory.py` fails if a loop has no row here or a row names a loop that is gone. (This record is
not a page of the published docs site, so "ADR 0002, Recovery by stage" is a repository path, not a link there.) Every loop is
`owner_only`, so the "next owner" is whichever process acquires the lease after the old one lapses
(`CALIBER_EXECUTION_LEASE_SECONDS`, default 30 s, plus one tick); a secondary never recovers anything. The loops are not
yet fenced per transition (see "Background loops"), so recovery rests on the per-row claims below, not on the fencing
token. The operator steps are in `docs/operate/recovery-runbook.md`.

| background loop | owner-only? | owner dies mid-tick | next owner resumes by | table that proves it |
| --- | --- | --- | --- | --- |
| `RefinementWorker` | yes | the job stays `running` and its `last_heartbeat_at` stops moving | the `JanitorTask` marks it `failed` once the heartbeat is older than `CALIBER_JANITOR_STALE_THRESHOLD_SECONDS` (300 s); it is **not** requeued, the refinement is run again by a person | `caliber_refinement_jobs`; `caliber_audit_log` (`reap_stale_job`) |
| `JanitorTask` | yes | a sweep is cut short; each reap is its own conditional `UPDATE`, so nothing is half-applied | the next owner's first sweep (every `CALIBER_JANITOR_INTERVAL_SECONDS`, 60 s) repeats it; a repeated retention prune deletes nothing more. Its second, fixed sweep returns a refinement job stuck in `applying` for 1800 s with no release operation to `candidate_ready` (`revert_stale_applying`) | `caliber_refinement_jobs`; `caliber_audit_log`; `caliber_workflow_runs` |
| `ReleaseReconcilerTask` | yes | an operation is left `applying` or `reconcile_required` (the provider alias may or may not have moved); there is no per-row claim | the next owner's sweep (60 s) compares the live alias with the intent's recorded before and after versions and settles it, and moves the refinement job from `applying` to `applied` when the release landed; a `prepared` operation is never settled by the loop (no provider call started: an operator decides) | `caliber_release_operations` |
| `CalibrationDrain` | yes | the job stays `running` with its claim; a tool may already have run | nothing, on purpose: a re-run could repeat a tool's side effect, so an operator abandons it or submits a new lineage-linked job | `caliber_calibration_jobs` |
| `WebhookDispatcher` | yes | events the owner had accepted and not yet delivered keep an accept row whose lease (at least 60 s) is no longer renewed | when the lease has lapsed, the next owner's start sweep and its recurring lease sweep turn each accepted row into a dead letter of kind `shutdown` (one transaction); an operator replays it, nothing redelivers by itself | `caliber_webhook_accepted_events`; `caliber_webhook_dead_letters` |
| `WorkflowRunWorker` | yes | the run stays `running` until its lease (`CALIBER_WORKFLOW_RUN_LEASE_SECONDS`, 60 s) lapses | `_recover_expired_leases` requeues it (`error_code` `lease_recovered`, event `workflow.run.recovered`) and a worker runs it again from the beginning, unless its summary already carries a resume checkpoint (a parked run is not `running`, so lease recovery never sees it); a webhook or API-request effect already claimed is not repeated (the ledger replays it or reports it `indeterminate`, and a non-final proposal gates the node); effects of other node types are not ledgered and can happen again | `caliber_workflow_runs`; `caliber_workflow_run_checkpoints`; `caliber_effect_ledger`; `caliber_effect_proposals` |
| `AriaPlanWorker` | yes | there is no claim: a step stays at the status its last commit left | the next owner's tick polls the steps still `waiting_job` and resumes their plans | `caliber_aria_plan_steps` |
| `KnowledgeBaseWorker` | yes | the build run stays `running` until its lease (`CALIBER_KNOWLEDGE_BUILD_LEASE_SECONDS`, 120 s) lapses | `_recover_expired_leases` sets it `queued` again ("worker lease expired; build re-queued for recovery", event `build_requeued`) | `caliber_knowledge_base_runs`; `caliber_knowledge_base_run_events` |
| `WorkspaceImportWorker` | yes | nothing was persisted (the revision and the job's `succeeded` commit together); the job stays `running` until its lease (`CALIBER_WORKSPACE_IMPORT_LEASE_SECONDS`, 300 s) lapses | `reconcile_expired_import_jobs` moves it to `reconcile_required`; it is not requeued, `retry_failed_import_job` refuses that state (it accepts only `failed`), and no operator action on the job exists yet: an operator queues a new import job for the same content, which is safe by digest | `caliber_workspace_import_jobs`; `caliber_workspace_revisions` |
| `WorkspaceReleaseEvaluator` | yes | a claimed evaluation remains `running` and its release remains `evaluating` if the process dies; the lease expires while the attempt history preserves the worker identity and any provider run identifiers | the next owner marks the expired attempt `orphaned`, increments the attempt number, and reclaims the parent row; heartbeat/completion are fenced to the current attempt, so a stale worker cannot settle it | `caliber_workspace_release_evaluations`; `caliber_workspace_release_evaluation_attempts`; `caliber_workspace_releases` |
| `EffectReconcilerTask` | yes | a pass is cut short; every move it makes is its own version-checked write in a transaction that compares the lease, and no lock or claim is held across a lookup, so nothing is half-applied | the next owner's first pass (every `CALIBER_EFFECT_RECONCILER_INTERVAL_SECONDS`, 15 s): a dispatch of the dead lease (a lower fencing token) becomes `unknown` (`abandoned_dispatch`) and every `unknown` effect is asked about by its logical key; nothing is re-sent. The same pass moves a dispatch left under the *current* lease for longer than `CALIBER_EFFECT_IN_FLIGHT_MAX_SECONDS` (900 s) to `unknown` (`in_flight_expired`). Each tick also finalizes governed-step token reservations still open past their step's deadline plus the grace as abandoned, at their whole reservation (B08.4); a reservation is never released | `caliber_effect_proposals`; `caliber_effect_receipts`; `caliber_step_token_reservations`; `caliber_audit_log` |
| `WorkflowSchedulerTask` | yes | a tick commits once, so a crash mid-tick persists none of that tick's runs | the unique `(workflow_id, source, idempotency_key)` on `cron:{alias}:{minute}` makes a repeat harmless; a minute that passes with no owner, or whose tick the new owner misses, is not fired later | `caliber_workflow_deployments`; `caliber_workflow_runs` |
| `OutboxPublisher` | yes | the claimed rows keep a lease (30 s) and the attempt was already counted | at lease expiry the rows are due again and the next owner's publisher redelivers them; the consumer's inbox key `(consumer, event_id)` drops a repeat; a row that runs out of `CALIBER_EVENT_OUTBOX_MAX_ATTEMPTS` is `dead` and kept | `caliber_event_outbox`; `caliber_event_inbox` |

The effect reconciler (`caliber/src/caliber/effects/reconcile.py`) is a function the lease holder calls with its captured
lease, and `EffectReconcilerTask` (B05.5d, #997) is the loop that calls it, so on main an `unknown` or abandoned effect is
asked about by key every pass without anyone having to. It runs only in the lease holder, hands `reconcile_pass` the exact
lease the controller holds, ends a pass between proposals the moment that lease is gone or replaced, and never renews it
(the controller does). What is not a loop is the effect proposal's recovery: it is never re-dispatched by any of them, the
state table has no edge from `unknown` back to `dispatched`.

### Protected paths

Each row of `docs/architecture/protected-effect-paths.md` and the module that owns it from the Beta on. The
inventory's D3 disposition decides the owner: `dispatch` rows are owned by the dispatcher (a `read` row gets a
recorded policy decision only; every other class also gets an EffectRecord and Receipt written there), `refuse`
rows are refused by the dispatcher when `CALIBER_PROFILE=reference` (`profile.py` is the switch, not the refuser),
and `exempt` rows stay with the module the row names.

| row | entry point | owning module | basis |
| --- | --- | --- | --- |
| EP-001 | `caliber/src/caliber/workflows/runtime.py::_perform_guarded_effect` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-002 | `caliber/src/caliber/workflows/runtime.py::_perform_guarded_effect` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-003 | `caliber/src/caliber/workflows/runtime.py::_default_webhook_sender` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-004 | `caliber/src/caliber/workflows/runtime.py::_execute_model_tool_call` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-005 | `caliber/src/caliber/workflows/runtime.py::_invoke_agent_tools` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-006 | `caliber/src/caliber/workflows/runtime.py::_run_node` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-007 | `caliber/src/caliber/workflows/runtime.py::_run_node` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | MCP nodes of manifest-backed plans are classified from the MCP server record and the administrator's tool policy: a `read` tool stays inline, every other call uses the dispatcher's `mcp` backend, and an unclassified, blocked or first-party database write tool is refused (EP-134) |
| EP-008 | `caliber/src/caliber/workflows/runtime.py::_bind_mcp_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | the inline MCP callable runs only a `read`-class tool under the reference profile and refuses every other (`EffectRefused`; the development profile runs it as before, not governed); an MCP tool node is routed before it by EP-134, and a model-chosen protected call is proposed by B04.6 (#768) |
| EP-009 | `caliber/src/caliber/workflows/runtime.py::_run_node` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-010 | `caliber/src/caliber/workflows/runtime.py::_run_node` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-011 | `caliber/src/caliber/workflows/runtime.py::_run_node` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-012 | `caliber/src/caliber/workflows/runtime.py::_run_external_app_entrypoint` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-013 | `caliber/src/caliber/workflows/runtime.py::_write_output_bucket_node` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-014 | `caliber/src/caliber/workflows/runtime.py::_write_output_folder_node` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-015 | `caliber/src/caliber/workflows/runtime.py::_sandboxed_registered_tool` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-016 | `caliber/src/caliber/workflows/runtime.py::bind_exported_tool` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-017 | `caliber/src/caliber/routes/workflow_versions.py::_run_workflow_version_sync` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-018 | `caliber/src/caliber/orchestrator/workflow_run_worker.py::_persist_run_artifacts` | `caliber/src/caliber/orchestrator/workflow_run_worker.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-019 | `caliber/src/caliber/workflows/tools.py::bind_registered_tool` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-020 | `caliber/src/caliber/tool_sandbox/service.py::LocalSubprocessToolSandbox.run_tool` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-021 | `caliber/src/caliber/tool_sandbox/service.py::LocalSubprocessToolSandbox.inspect_tool` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-022 | `caliber/src/caliber/tool_sandbox/service.py::LocalSubprocessToolSandbox.run_tests` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-023 | `caliber/src/caliber/tool_sandbox/service.py::LocalSubprocessToolSandbox._run_payload` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-024 | `caliber/src/caliber/tool_sandbox/service.py::_load_backend_factory` | `caliber/src/caliber/tool_sandbox/service.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-025 | `caliber/src/caliber/tool_sandbox/_runner.py::_namespace` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-026 | `caliber/src/caliber/tool_sandbox/_runner.py::_resolve_callable` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-027 | `caliber/src/caliber/tool_sandbox/server.py::run_tool` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-028 | `caliber/src/caliber/tool_sandbox/server.py::run_tests` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-029 | `caliber/src/caliber/tool_sandbox/__main__.py::main` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-030 | `caliber/src/caliber/integrations/openapi/executor.py::execute_openapi_http_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | **read-only public entry; writes only via the private transport**: the public entry sends only a read (`GET`, `HEAD`, `OPTIONS`) and refuses any other method with `unsupported_effect_path`; a write reaches the network only through the private `_send_raw`, which the dispatcher's private backends call (the `openapi_http` backend, EP-133, and the document request, whose proposals the dispatcher records with an EffectRecord and Receipt) and nothing else names (the exempt preview route, EP-033, no longer does: in the reference profile it uses this public entry, #1072); the development profile reaches it through two named legacy entries that the reference profile never uses: `bind_legacy_openapi_http_tool` for a plan with no governed context (#1068) and `execute_legacy_openapi_draft_preview` for the exempt preview route (#1072); the callers are pinned by `tests/test_effect_path_matrix.py` |
| EP-031 | `caliber/src/caliber/integrations/openapi/executor.py::bind_openapi_http_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | **read-only**: this callable wraps the public entry (EP-030), so it can read and a protected method is refused with `unsupported_effect_path`; it is not itself routed through the dispatcher, and its writes belong to EP-133 (workflow tool nodes), #768 (model-chosen calls) and #1051 (exports) |
| EP-032 | `caliber/src/caliber/integrations/openapi/executor.py::_oauth_access_token` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-033 | `caliber/src/caliber/routes/openapi_integrations.py::preview_openapi_tool_draft` | `caliber/src/caliber/routes/openapi_integrations.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it. **Read-only in the reference profile (#1072)**: there it sends through the public entry (EP-030), so a non-read operation is refused with `409` `unsupported_effect_path` (audited, nothing sent) and an operator can no longer set `allow_in_preview` on a draft and fire a `POST`/`DELETE` with no second person, proposal, lease or receipt. **Development profile**: unchanged, any method through the named legacy entry `execute_legacy_openapi_draft_preview` (a human-fired call gated by `allow_in_preview`), which only this route may name |
| EP-034 | `caliber/src/caliber/assistant/capabilities.py::invoke_openapi_published_tool` | `caliber/src/caliber/effects/` dispatcher (#520; target owner, not routed yet, tracked in #1063) | **not routed, but refused when protected**: Aria's call has no workflow run to propose into, and it uses the public executor entry, which refuses a protected method with `unsupported_effect_path` (so an exposed write tool, or a tool declared `read` whose operation is a `POST`, sends nothing); a read tool still runs. Routing its writes through the dispatcher needs a run-less authority and is decided in #1063 (the workflow tool-node path of the same tools is EP-133) |
| EP-035 | `caliber/src/caliber/mcp_gateway.py::invoke_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | the raw async transport: a protected call reaches it only through the dispatcher's `mcp` backend (EP-135); the other callers are the operator routes EP-039 and EP-040, which are exempt |
| EP-036 | `caliber/src/caliber/mcp_gateway.py::invoke_tool_sync` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | under the reference profile refuses every tool whose trusted classification is not `read` (`EffectRefused`) and sends nothing (the development profile lets it through as before); the ungated body (`invoke_tool_raw_sync`) is called only by the dispatcher's `mcp` backend (EP-135) |
| EP-037 | `caliber/src/caliber/mcp_gateway.py::invoke_tool_by_server_id_sync` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | resolves the server and calls EP-036, so under the reference profile it runs only `read`-class tools and refuses the rest |
| EP-038 | `caliber/src/caliber/mcp_gateway.py::discover_tools` | `caliber/src/caliber/mcp_gateway.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-039 | `caliber/src/caliber/routes/mcp_servers.py::invoke_tool` | `caliber/src/caliber/routes/mcp_servers.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-040 | `caliber/src/caliber/routes/mcp_servers.py::calibrate_mcp_tool` | `caliber/src/caliber/routes/mcp_servers.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-041 | `caliber/src/caliber/workflows/export_runtime.py::execute_exported_workflow` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-042 | `caliber/src/caliber/workflows/export_runtime.py::_default_knowledge_build_runner` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-043 | `caliber/src/caliber/workflows/compiler.py::generate_python` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-044 | `caliber/src/caliber/routes/workflow_versions.py::export_python_route` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-045 | `caliber/src/caliber/routes/workflow_versions.py::export_deployment_bundle_route` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-046 | `caliber/src/caliber/routes/workflow_versions.py::export_manifest_route` | `caliber/src/caliber/routes/workflow_versions.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-047 | `caliber/src/caliber/workflows/file_tools.py::write_workdir_file` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-048 | `caliber/src/caliber/workflows/file_tools.py::create_artifact` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-049 | `caliber/src/caliber/storage/service.py::WorkingDirectoryService.write_artifact` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-050 | `caliber/src/caliber/storage/service.py::WorkingDirectoryService.cleanup_run_files` | `caliber/src/caliber/storage/service.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-051 | `caliber/src/caliber/storage/s3.py::S3StorageBackend.write_bytes` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-052 | `caliber/src/caliber/storage/s3.py::S3StorageBackend.delete` | `caliber/src/caliber/storage/s3.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-053 | `caliber/src/caliber/storage/s3.py::S3StorageBackend.signed_url` | `caliber/src/caliber/storage/s3.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-054 | `caliber/src/caliber/workflows/demo_tools.py::sandbox_python` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-055 | `caliber/src/caliber/workflows/demo_tools.py::read_text_file` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-056 | `caliber/src/caliber/workflows/ingestion_tools.py::_ocr_pdf` | `caliber/src/caliber/effects/` dispatcher (#520) | routed through the dispatcher: a recorded policy decision only, no EffectRecord or Receipt (ADR 0001) |
| EP-057 | `caliber/src/caliber/memory/service.py::MemoryService.add` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-058 | `caliber/src/caliber/memory/service.py::MemoryService.delete_all` | `caliber/src/caliber/memory/service.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-059 | `caliber/src/caliber/knowledge/worker.py::KnowledgeBaseWorker._execute_run` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-060 | `caliber/src/caliber/knowledge/service.py::KnowledgeBaseService._put_json` | `caliber/src/caliber/knowledge/service.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-061 | `caliber/src/caliber/knowledge/service.py::KnowledgeBaseService._best_effort_delete_output_prefix` | `caliber/src/caliber/knowledge/service.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-062 | `caliber/src/caliber/apply.py::apply_candidate` | `caliber/src/caliber/apply.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-063 | `caliber/src/caliber/apply.py::_apply_prompt_alias` | `caliber/src/caliber/apply.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-064 | `caliber/src/caliber/apply.py::_apply_bundle` | `caliber/src/caliber/apply.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-065 | `caliber/src/caliber/apply.py::_apply_workflow_manifest` | `caliber/src/caliber/apply.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-066 | `caliber/src/caliber/promoter.py::MLflowPromoter.promote` | `caliber/src/caliber/promoter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-067 | `caliber/src/caliber/promoter.py::MLflowPromoter.rollback` | `caliber/src/caliber/promoter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-068 | `caliber/src/caliber/promoter.py::SkillPromoter.promote` | `caliber/src/caliber/promoter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-069 | `caliber/src/caliber/bundle.py::promote_bundle` | `caliber/src/caliber/bundle.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-070 | `caliber/src/caliber/release_operations.py::execute_prompt_alias_release` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-071 | `caliber/src/caliber/routes/prompts.py::register_prompt_version` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-072 | `caliber/src/caliber/routes/prompts.py::set_prompt_alias_version` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-073 | `caliber/src/caliber/routes/prompts.py::set_prompt_alias` | `caliber/src/caliber/routes/prompts.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-074 | `caliber/src/caliber/routes/prompts.py::delete_prompt` | `caliber/src/caliber/routes/prompts.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-075 | `caliber/src/caliber/assistant/publisher.py::AssistantPublisher._publish_prompt` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-076 | `caliber/src/caliber/assistant/publisher.py::AssistantPublisher._publish_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-077 | `caliber/src/caliber/assistant/service.py::AssistantService._publish_draft_as` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-078 | `caliber/src/caliber/assistant/service.py::AssistantService._run_tool_sandbox_tests` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-079 | `caliber/src/caliber/assistant/agent_tools.py::AssistantAgentToolset._t_run_tool_sandbox` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-080 | `caliber/src/caliber/assistant/agent_tools.py::AssistantAgentToolset._t_evaluate_tool_draft` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-081 | `caliber/src/caliber/assistant/agent_tools.py::AssistantAgentToolset._t_run_workflow` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-082 | `caliber/src/caliber/workspace_release_prompt_adapter.py::PromptWorkspaceResourceAdapter._promote` | `caliber/src/caliber/workspace_release_prompt_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-083 | `caliber/src/caliber/workspace_release_workflow_adapter.py::WorkflowWorkspaceResourceAdapter._rotate` | `caliber/src/caliber/workspace_release_workflow_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-084 | `caliber/src/caliber/workspace_release_tool_adapter.py::ToolWorkspaceResourceAdapter._verify` | `caliber/src/caliber/workspace_release_tool_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-085 | `caliber/src/caliber/workspace_release_skill_adapter.py::SkillWorkspaceResourceAdapter._verify` | `caliber/src/caliber/workspace_release_skill_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-086 | `caliber/src/caliber/workspace_release_judge_adapter.py::JudgeWorkspaceResourceAdapter._verify` | `caliber/src/caliber/workspace_release_judge_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-087 | `caliber/src/caliber/workspace_release_eval_dataset_adapter.py::EvalDatasetWorkspaceResourceAdapter._verify` | `caliber/src/caliber/workspace_release_eval_dataset_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-088 | `caliber/src/caliber/workspace_release_mcp_server_adapter.py::McpServerWorkspaceResourceAdapter._verify` | `caliber/src/caliber/workspace_release_mcp_server_adapter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-089 | `caliber/src/caliber/workflows/promoter.py::promote` | `caliber/src/caliber/workflows/promoter.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-090 | `caliber/src/caliber/github_source_control.py::GitHubSourceControlProvider.publish_status` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-091 | `caliber/src/caliber/github_http_transport.py::HTTPXGitHubTransport.request` | `caliber/src/caliber/github_http_transport.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-092 | `caliber/src/caliber/github_webhook_reconciliation.py::redeliver` | `caliber/src/caliber/github_webhook_reconciliation.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-093 | `caliber/src/caliber/github_app_auth.py::GitHubAppInstallationTokenProvider.token` | `caliber/src/caliber/github_app_auth.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-094 | `caliber/src/caliber/routes/object_store.py::upload_object` | `caliber/src/caliber/routes/object_store.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-095 | `caliber/src/caliber/routes/object_store.py::delete_object` | `caliber/src/caliber/routes/object_store.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-096 | `caliber/src/caliber/routes/object_store.py::delete_objects_batch` | `caliber/src/caliber/routes/object_store.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-097 | `caliber/src/caliber/routes/object_store.py::create_folder` | `caliber/src/caliber/routes/object_store.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-098 | `caliber/src/caliber/routes/object_store.py::download_object` | `caliber/src/caliber/routes/object_store.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-099 | `caliber/src/caliber/routes/object_store.py::import_object_to_project` | `caliber/src/caliber/routes/object_store.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-100 | `caliber/src/caliber/routes/assistant.py::upload_attachment` | `caliber/src/caliber/routes/assistant.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-101 | `caliber/src/caliber/observability/logging.py::S3LogHandler.flush` | `caliber/src/caliber/observability/logging.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-102 | `caliber/src/caliber/routes/system_effects.py::replay_webhook_dead_letter` | `caliber/src/caliber/routes/system_effects.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-103 | `caliber/src/caliber/events/webhooks.py::WebhookDispatcher._post` | `caliber/src/caliber/events/webhooks.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-104 | `caliber/src/caliber/routes/tools.py::_invoke_tool_under_preview_policy` | `caliber/src/caliber/routes/tools.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-105 | `caliber/src/caliber/routes/tools.py::test_run_tool` | `caliber/src/caliber/routes/tools.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-106 | `caliber/src/caliber/routes/skills.py::get_skill_package_zip` | `caliber/src/caliber/routes/skills.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-107 | `caliber/src/caliber/routes/audit.py::export_audit_log` | `caliber/src/caliber/routes/audit.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-108 | `caliber/src/caliber/routes/projects.py::download_project_file` | `caliber/src/caliber/routes/projects.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-109 | `caliber/src/caliber/routes/files.py::get_file_content` | `caliber/src/caliber/routes/files.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-110 | `caliber/src/caliber/routes/gateway.py::create_gateway_guardrail` | `caliber/src/caliber/routes/gateway.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-111 | `caliber/src/caliber/routes/system_services.py::_probe_http` | `caliber/src/caliber/routes/system_services.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-112 | `caliber/src/caliber/resolver.py::PromptResolver.resolve` | `caliber/src/caliber/resolver.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-113 | `caliber/src/caliber/assistant/ollama_engine.py::OllamaAssistantEngine.run_turn` | `caliber/src/caliber/assistant/ollama_engine.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-114 | `caliber/src/caliber/mcp_servers/db/connection.py::execute` | `caliber/src/caliber/effects/` dispatcher (#520); switch: `profile.py` | refused in the reference profile with `unsupported_in_reference_profile` |
| EP-115 | `caliber/src/caliber/review/writeback.py::MLflowReviewWriteBackClient.write_answers` | `caliber/src/caliber/review/writeback.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-116 | `caliber/src/caliber/routes/observability.py::post_feedback` | `caliber/src/caliber/routes/observability.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-117 | `caliber/src/caliber/eval/dataset_sync.py::MLflowDatasetSyncClient.sync_dataset` | `caliber/src/caliber/eval/dataset_sync.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-118 | `caliber/src/caliber/llm/openai_agents.py::OpenAIAgentsLLMProvider._generate_candidate_gepa` | `caliber/src/caliber/llm/openai_agents.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-119 | `caliber/src/caliber/orchestrator/candidate.py::_register_candidate_prompt_draft` | `caliber/src/caliber/orchestrator/candidate.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-120 | `caliber/src/caliber/eval/mlflow_runner.py::MLflowEvalProvider._run_eval_pass` | `caliber/src/caliber/eval/mlflow_runner.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-121 | `caliber/src/caliber/extensibility/entrypoints.py::_load` | `caliber/src/caliber/extensibility/entrypoints.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-122 | `caliber/src/caliber/events/redis_bus.py::RedisEventBus._publish_remote` | `caliber/src/caliber/events/redis_bus.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-123 | `caliber/src/caliber/events/nats_bus.py::NatsEventBus._publish_remote` | `caliber/src/caliber/events/nats_bus.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-124 | `caliber/src/caliber/integrations/openapi/loader.py::load_spec_from_url` | `caliber/src/caliber/integrations/openapi/loader.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-125 | `caliber/src/caliber/workflows/memory_tools.py::memory_search` | `caliber/src/caliber/effects/` dispatcher (#520) | routed through the dispatcher: a recorded policy decision only, no EffectRecord or Receipt (ADR 0001) |
| EP-126 | `caliber/src/caliber/workflows/file_tools.py::read_workdir_file` | `caliber/src/caliber/effects/` dispatcher (#520) | routed through the dispatcher: a recorded policy decision only, no EffectRecord or Receipt (ADR 0001) |
| EP-127 | `caliber/src/caliber/workflows/demo_tools.py::grok_parse` | `caliber/src/caliber/effects/` dispatcher (#520) | routed through the dispatcher: a recorded policy decision only, no EffectRecord or Receipt (ADR 0001) |
| EP-128 | `caliber/src/caliber/routes/assistant.py::_list_ollama_models` | `caliber/src/caliber/routes/assistant.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-129 | `caliber/src/caliber/storage/s3.py::S3StorageBackend.copy` | `caliber/src/caliber/storage/s3.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-130 | `caliber/src/caliber/workflows/step_openai_agents.py::_build_tools` | `caliber/src/caliber/effects/` dispatcher (#520) | routed through the dispatcher: a recorded policy decision only, no EffectRecord or Receipt (ADR 0001) |
| EP-131 | `caliber/src/caliber/workflows/runtime.py::_perform_http_effect` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | governed manifest calls use the dispatcher; legacy plans retain the ledger-backed `_perform_guarded_effect` path |
| EP-132 | `caliber/src/caliber/workflows/runtime.py::_perform_registered_tool_effect` | `caliber/src/caliber/effects/` dispatcher (#520, #741) | registered Python tool nodes are routed from the tool's registry row: protected calls use the dispatcher's `registered_tool` backend, read-class calls stay inline, refused built-ins never run |
| EP-133 | `caliber/src/caliber/workflows/runtime.py::_perform_openapi_tool_effect` | `caliber/src/caliber/effects/` dispatcher (#520, #750) | registered OpenAPI tool nodes are routed from the tool's registry row: a `GET` of a read row stays inline, every other call uses the dispatcher's `openapi_http` backend, and a tool with no readable OpenAPI row is refused |
| EP-134 | `caliber/src/caliber/workflows/runtime.py::_perform_mcp_tool_effect` | `caliber/src/caliber/effects/` dispatcher (#520, #759) | MCP tool nodes and MCP nodes are routed from the MCP server record, the administrator's tool policy and the inventory (never what the server reports): a `read` tool stays inline, every other call uses the dispatcher's `mcp` backend, and an unclassified, blocked, unreadable or first-party database write tool is refused |
| EP-135 | `caliber/src/caliber/effects/dispatcher.py::_McpToolBackend.perform` | `caliber/src/caliber/effects/dispatcher.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |

## Consequences

- #517, #518, #520, #522, #523, #527, #536 and #538 each create the module named for their record; a PR that
  writes one of these records from another module is rejected in review, and the dispatcher's path matrix
  (#520) is parametrized from the third table's `dispatch` rows.
- The ADR index test checks that every inventory row appears in the third table, so a new protected path cannot
  be added without an owner.
- A lease holder that dies releases ownership by expiry; moving a loop to another process is a lease transfer,
  not a code change. Until #536 lands the platform runs as it does today, with per-job leases only.
- `exempt` is an owner decision, not a safe harbour: the inventory's notes say why each exempt row cannot be
  reached by a model proposal, and a change that makes one reachable moves the row to `dispatch` in the same PR.

## Appendix: worker ownership classification (B13.1, #660)

The same thirteen loops, classified for the lease: `owner_only` means only the execution-owner lease holder runs the
loop and it stops claiming when the lease is lost; every other replica serves requests only. `any_process` would
mean the loop is safe on every replica at once. The classification is the `ownership` field of
`caliber/src/caliber/observability/worker_inventory.py`, and `caliber/tests/test_worker_inventory.py` fails if this
table and the inventory disagree. All thirteen are `owner_only`: the claim in the last column makes an overlap harmless today
but does not fence a stale process whose lease expired mid-transition, and two loops (`AriaPlanWorker`,
`ReleaseReconcilerTask`) have no per-row claim at all. A loop moves to `any_process` only by an amendment to this record that shows it safe under fencing.

| background loop | ownership | claim mechanism today |
| --- | --- | --- |
| `RefinementWorker` | `owner_only` | atomic `UPDATE ... RETURNING` claim of the oldest queued job |
| `WorkflowRunWorker` | `owner_only` | per-run lease (`workflow_run_lease_seconds`) |
| `AriaPlanWorker` | `owner_only` | none: polls parked plan steps and resumes them |
| `KnowledgeBaseWorker` | `owner_only` | per-build lease (`knowledge_build_lease_seconds`) |
| `WorkflowSchedulerTask` | `owner_only` | unique `(workflow_id, source, idempotency_key)` on `cron:{alias}:{minute}` |
| `JanitorTask` | `owner_only` | conditional `UPDATE ... WHERE` per stale row, audit entry only for the winner; the retention prune is unguarded and harmless when repeated |
| `ReleaseReconcilerTask` | `owner_only` | none: settles incomplete release operations by scan |
| `CalibrationDrain` | `owner_only` | conditional `UPDATE ... WHERE status = 'queued'` claim |
| `WebhookDispatcher` | `owner_only` | accept lease (at least 60 s, covering the retry window) plus an in-process in-flight claim |
| `OutboxPublisher` | `owner_only` | per-row lease (`claimed_by`, `lease_expires_at`): a conditional UPDATE claims each row (exclusive on SQLite too; `FOR UPDATE SKIP LOCKED` on PostgreSQL avoids the contention) and only the claimer can settle it; redelivery is deduplicated by the consumer's inbox key `(consumer, event_id)` |
| `WorkspaceImportWorker` | `owner_only` | per-job lease (`workspace_import_lease_seconds`) |
| `WorkspaceReleaseEvaluator` | `owner_only` | conditional `UPDATE ... WHERE status = 'queued'` claim (`claim_workspace_release_evaluation`) with a per-attempt lease (`workspace_release_evaluator_lease_seconds`), made inside a transaction fenced by `assert_fenced` |
| `EffectReconcilerTask` | `owner_only` | none to claim: each proposal moves by a compare-and-set on its `version`, written in a transaction that compares the exact owner lease (`fence_owner`); a lookup holds no lock |

The event bus is not in the worker inventory and is classified by its own row in the table above.

One consequence to plan for: `WebhookDispatcher` runs only on the lease holder, so an event published on an API-only
replica reaches it only if the event bus crosses processes (the database, Redis or NATS bus). An in-process bus on
a multi-replica deployment would never deliver it, so a multi-replica reference deployment must not use one.
