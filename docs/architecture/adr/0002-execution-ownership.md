# ADR 0002 — Execution ownership

## Status

Accepted, 2026-10-26. Cites ADR 0001 (the reference profile) and `docs/architecture/protected-effect-paths.md`
(the protected-effect inventory, #627).

## Context

Four things today have no single owner. (1) Control records the EGI program adds (run manifest, capability
envelope, policy decision, approval, effect record, receipt, acceptance decision, evidence package, outbox) have
no agreed writer: two modules could each persist "the" receipt. (2) Ten background workers and an event bus are
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

Started in `caliber/src/caliber/server.py::create_app`'s lifespan. The twelve workers are listed by
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
| `WorkspaceReleaseEvaluator` | execution-owner lease (B13, #536) | Claims queued Workspace release evaluations, oldest first, fenced by the lease; resolving and running the plan arrives with B10.2. |
| `EventBus` (database, Redis or NATS) | every process (fans events out; takes no effect) | publisher and poll tasks (database bus) or consumer tasks (Redis, NATS) deliver platform events to subscribers; publishing becomes idempotent through the outbox (#536) |

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
| EP-007 | `caliber/src/caliber/workflows/runtime.py::_run_node` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-008 | `caliber/src/caliber/workflows/runtime.py::_bind_mcp_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
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
| EP-030 | `caliber/src/caliber/integrations/openapi/executor.py::execute_openapi_http_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-031 | `caliber/src/caliber/integrations/openapi/executor.py::bind_openapi_http_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-032 | `caliber/src/caliber/integrations/openapi/executor.py::_oauth_access_token` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-033 | `caliber/src/caliber/routes/openapi_integrations.py::preview_openapi_tool_draft` | `caliber/src/caliber/routes/openapi_integrations.py` (unchanged) | exempt: see the inventory note for why no model proposal or workflow node reaches it |
| EP-034 | `caliber/src/caliber/assistant/capabilities.py::invoke_openapi_published_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-035 | `caliber/src/caliber/mcp_gateway.py::invoke_tool` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-036 | `caliber/src/caliber/mcp_gateway.py::invoke_tool_sync` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
| EP-037 | `caliber/src/caliber/mcp_gateway.py::invoke_tool_by_server_id_sync` | `caliber/src/caliber/effects/` dispatcher (#520, #522) | routed through the dispatcher, which writes the EffectRecord and Receipt; an entry point reached only through another row is covered by that row (see the inventory note) |
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

The same twelve loops, classified for the lease: `owner_only` means only the execution-owner lease holder runs the
loop and it stops claiming when the lease is lost; every other replica serves requests only. `any_process` would
mean the loop is safe on every replica at once. The classification is the `ownership` field of
`caliber/src/caliber/observability/worker_inventory.py`, and `caliber/tests/test_worker_inventory.py` fails if this
table and the inventory disagree. All twelve are `owner_only`: the claim in the last column makes an overlap harmless today
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

The event bus is not in the worker inventory and is classified by its own row in the table above.

One consequence to plan for: `WebhookDispatcher` runs only on the lease holder, so an event published on an API-only
replica reaches it only if the event bus crosses processes (the database, Redis or NATS bus). An in-process bus on
a multi-replica deployment would never deliver it, so a multi-replica reference deployment must not use one.
