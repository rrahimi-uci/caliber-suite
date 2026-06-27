/**
 * NodeDetailPanel — read-only inspector shown when clicking a node
 * on the WorkflowDetail graph tab.
 *
 * Displays every configured property for the selected node: type,
 * ports (inputs/outputs with data-type colored dots), and all
 * type-specific fields (instructions, tools, guardrail settings,
 * router branches, file/folder paths, etc.).
 */

import type {
  ManifestNode,
  ValidationReport,
  WorkflowComponent,
  WorkflowManifest,
} from "@/api/workflowTypes";
import {
  nodeColor,
  nodeGuide,
  nodeLabel,
  nodeValidationIssues,
  portColor,
} from "@/lib/workflowGraph";
import { NodeIcon } from "@/components/workflows/NodeIcon";
import { WorkflowComponentSchemaSummary } from "@/components/workflows/WorkflowComponentSchemaSummary";

interface NodeDetailPanelProps {
  manifest: WorkflowManifest;
  nodeId: string;
  validationReport?: ValidationReport | null;
  componentSpec?: WorkflowComponent | null;
  onClose: () => void;
}

export function NodeDetailPanel({
  manifest,
  nodeId,
  validationReport,
  componentSpec,
  onClose,
}: NodeDetailPanelProps): JSX.Element {
  const node = manifest.nodes[nodeId];
  if (!node) {
    return (
      <div className="p-4 text-sm text-slate-400">
        Node not found.
      </div>
    );
  }

  const color = nodeColor(node.type);
  const guide = nodeGuide(node, componentSpec ?? null, manifest);
  const issues = nodeValidationIssues(validationReport, node.id);
  const inputPorts = Object.entries(node.inputs ?? {});
  const outputPorts = Object.entries(node.outputs ?? {});
  const manifestTools = (manifest.tools ?? {}) as Record<string, unknown>;
  const toolBinding =
    node.type === "tool"
      && typeof node.tool_name === "string"
      && node.tool_name
      && typeof manifestTools[node.tool_name] === "object"
      && manifestTools[node.tool_name] !== null
      ? (manifestTools[node.tool_name] as Record<string, unknown>)
      : null;

  // Collect incoming and outgoing edges for this node
  const incomingEdges = manifest.edges.filter((e) => e.to === nodeId);
  const outgoingEdges = manifest.edges.filter((e) => e.from === nodeId);

  return (
    <div
      data-testid="node-detail-panel"
      className="animate-fade-in flex flex-col h-full overflow-y-auto"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200/60">
        <div className="flex items-center gap-2.5 min-w-0">
          <span
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
            style={{ backgroundColor: `${color}15`, color }}
          >
            <NodeIcon type={node.type} size={18} />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-bold text-slate-900 truncate">
              {nodeLabel(node)}
            </div>
            <div
              className="text-[10px] font-semibold uppercase tracking-wider"
              style={{ color }}
            >
              {node.type.replace(/_/g, " ")}
            </div>
          </div>
        </div>
        <button
          type="button"
          data-testid="node-detail-close"
          onClick={onClose}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600"
          aria-label="Close"
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M18 6L6 18M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {/* ─── Identity ─── */}
        <Section title="Identity">
          <Row label="ID" value={node.id} mono />
          <Row label="Type" value={node.type} />
          {node.name && <Row label="Name" value={node.name} />}
          {typeof node.model === "string" && <Row label="Model" value={node.model} />}
        </Section>

        <Section title="Guide">
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
            {guide.summary}
          </div>
          {guide.tips.length > 0 && (
            <div className="space-y-1.5">
              {guide.tips.map((tip) => (
                <div
                  key={tip}
                  className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600"
                >
                  {tip}
                </div>
              ))}
            </div>
          )}
          {guide.checks.length > 0 && (
            <div className="space-y-1.5">
              {guide.checks.map((check) => (
                <div
                  key={check.label}
                  className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-flex h-5 min-w-5 items-center justify-center rounded-full text-[10px] font-semibold ${
                        check.satisfied
                          ? "bg-emerald-100 text-emerald-700"
                          : "bg-amber-100 text-amber-700"
                      }`}
                    >
                      {check.satisfied ? "OK" : "!"}
                    </span>
                    <span className="font-medium text-slate-800">{check.label}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-slate-500">{check.help}</div>
                </div>
              ))}
            </div>
          )}
          {issues.length > 0 && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {issues.map((issue) => (
                <div key={`${issue.code}-${issue.path}`} className="py-0.5">
                  {issue.message}
                </div>
              ))}
            </div>
          )}
        </Section>

        {componentSpec && (
          <Section title="Runtime schema">
            <WorkflowComponentSchemaSummary component={componentSpec} />
          </Section>
        )}

        {/* ─── Instructions (agent) ─── */}
        {node.type === "agent" && node.instructions && (
          <Section title="Instructions">
            {node.instructions.type === "inline" ? (
              <pre className="whitespace-pre-wrap text-xs font-mono text-slate-700 bg-slate-50 rounded-lg border border-slate-200 px-3 py-2 max-h-48 overflow-y-auto leading-relaxed">
                {node.instructions.text}
              </pre>
            ) : (
              <Row label="Prompt ref" value={node.instructions.ref} mono />
            )}
          </Section>
        )}

        {/* ─── Tools (agent) ─── */}
        {node.type === "agent" && node.tools && node.tools.length > 0 && (
          <Section title={`Tools (${node.tools.length})`}>
            <div className="flex flex-wrap gap-1.5">
              {node.tools.map((t) => (
                <span
                  key={t}
                  className="inline-flex items-center rounded-md bg-caliber-50 px-2 py-0.5 text-[11px] font-medium text-caliber-700 ring-1 ring-caliber-200/60"
                >
                  {t}
                </span>
              ))}
            </div>
          </Section>
        )}

        {/* ─── Handoffs (agent) ─── */}
        {node.type === "agent" && node.handoffs && node.handoffs.length > 0 && (
          <Section title={`Handoffs (${node.handoffs.length})`}>
            <div className="space-y-2">
              {node.handoffs.map((h, i) => (
                <div key={i} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
                  <div className="font-medium text-slate-800">→ {h.target}</div>
                  {h.description && (
                    <div className="mt-0.5 text-slate-500">{h.description}</div>
                  )}
                  {h.condition && (
                    <div className="mt-1 font-mono text-[10px] text-slate-400">
                      if: {h.condition}
                    </div>
                  )}
                  {h.input_filter && (
                    <div className="mt-1 font-mono text-[10px] text-slate-400">
                      input filter: {h.input_filter}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* ─── File Input ─── */}
        {node.type === "start" && node.trigger && (
          <Section title="Trigger">
            <Row label="Mode" value={String(node.trigger.mode ?? "manual")} />
            {node.trigger.mode === "event" && (
              <Row label="Event" value={node.trigger.event_name || "—"} mono />
            )}
            {node.trigger.mode === "cron" && (
              <>
                <Row label="Cron" value={node.trigger.cron || "—"} mono />
                <Row label="Timezone" value={node.trigger.timezone || "UTC"} />
              </>
            )}
            {node.trigger.mode !== "manual" && (
              <>
                <Row label="Target" value={node.trigger.alias || "prod"} />
                <Row label="Enabled" value={node.trigger.enabled !== false ? "Yes" : "No"} />
              </>
            )}
          </Section>
        )}

        {node.type === "file_input" && (
          <Section title="File source">
            <Row label="Path" value={typeof node.path === "string" ? node.path : "—"} mono />
            <Row label="Max bytes" value={String(node.max_bytes ?? 200000)} />
            <Row label="Encoding" value={typeof node.encoding === "string" ? node.encoding : "utf-8"} />
          </Section>
        )}

        {/* ─── Folder Input ─── */}
        {node.type === "folder_input" && (
          <Section title="Folder source">
            <Row label="Path" value={typeof node.path === "string" ? node.path : "—"} mono />
            <Row label="Pattern" value={typeof node.pattern === "string" ? node.pattern : "**/*"} mono />
            <Row label="Recursive" value={node.recursive !== false ? "Yes" : "No"} />
            <Row label="Max files" value={String(node.max_files ?? 50)} />
            <Row label="Bytes/file" value={String(node.max_bytes_per_file ?? 100000)} />
            <Row label="Encoding" value={typeof node.encoding === "string" ? node.encoding : "utf-8"} />
          </Section>
        )}

        {node.type === "input_bucket" && (
          <Section title="Input bucket">
            <Row label="Bucket" value={typeof node.bucket === "string" && node.bucket ? node.bucket : "—"} mono />
            <Row label="Prefix" value={typeof node.prefix === "string" && node.prefix ? node.prefix : "—"} mono />
            <Row label="Recursive" value={node.recursive !== false ? "Yes" : "No"} />
            <Row label="Max objects" value={String(node.max_files ?? 50)} />
            <Row label="Bytes/object" value={String(node.max_bytes_per_file ?? 100000)} />
          </Section>
        )}

        {node.type === "output_bucket" && (
          <Section title="Output bucket">
            <Row label="Bucket" value={typeof node.bucket === "string" && node.bucket ? node.bucket : "—"} mono />
            <Row label="Prefix" value={typeof node.prefix === "string" && node.prefix ? node.prefix : "—"} mono />
            <Row label="Overwrite" value={node.overwrite !== false ? "Yes" : "No"} />
          </Section>
        )}

        {node.type === "output_folder" && (
          <Section title="Output folder">
            <Row label="Path" value={typeof node.path === "string" && node.path ? node.path : "—"} mono />
            <Row label="Overwrite" value={node.overwrite !== false ? "Yes" : "No"} />
          </Section>
        )}

        {node.type === "wait_until" && (
          <Section title="Wait until">
            <Row label="Until" value={typeof node.wait_until === "string" ? node.wait_until : "—"} mono />
            <Row label="Timezone" value={typeof node.timezone === "string" ? node.timezone : "UTC"} />
          </Section>
        )}

        {node.type === "wait_for_event" && (
          <Section title="Wait for event">
            <Row label="Event name" value={typeof node.event_name === "string" ? node.event_name : "—"} mono />
            <Row label="Correlation key" value={typeof node.correlation_key === "string" ? node.correlation_key : "—"} mono />
            <Row
              label="Timeout (s)"
              value={typeof node.timeout_seconds === "number" ? String(node.timeout_seconds) : "No timeout"}
            />
          </Section>
        )}

        {node.type === "for_each" && (
          <Section title="For each settings">
            <Row label="Target" value={typeof node.target_node_id === "string" && node.target_node_id ? node.target_node_id : "—"} mono />
            <Row label="Items port" value={typeof node.item_input_port === "string" ? node.item_input_port : "items"} mono />
            <Row label="Max items" value={String(node.max_items ?? 100)} />
          </Section>
        )}

        {node.type === "loop" && (
          <Section title="Loop settings">
            <Row label="Target" value={typeof node.target_node_id === "string" && node.target_node_id ? node.target_node_id : "—"} mono />
            <Row label="Max iterations" value={String(node.max_iterations ?? 10)} />
            <Row
              label="Stop condition"
              value={typeof node.stop_condition === "string" && node.stop_condition ? node.stop_condition : "—"}
              mono
            />
          </Section>
        )}

        {node.type === "join" && (
          <Section title="Join settings">
            <Row label="Mode" value={typeof node.mode === "string" ? node.mode : "all"} />
          </Section>
        )}

        {node.type === "parallel" && (
          <Section title="Parallel">
            <Row label="Behavior" value="Fan out to downstream edges" />
          </Section>
        )}

        {node.type === "error_boundary" && (
          <Section title="Error boundary">
            <Row label="Target" value={typeof node.target_node_id === "string" && node.target_node_id ? node.target_node_id : "—"} mono />
            <Row label="Compensation" value={typeof node.compensate_with === "string" && node.compensate_with ? node.compensate_with : "—"} mono />
            <Row label="Fallback" value={typeof node.fallback_text === "string" ? node.fallback_text : ""} />
          </Section>
        )}

        {node.type === "subworkflow" && (
          <Section title="Subworkflow">
            <Row label="Workflow ID" value={typeof node.workflow_id === "string" ? node.workflow_id : "—"} mono />
            <Row label="Alias" value={typeof node.alias === "string" ? node.alias : "prod"} />
            <Row label="Timeout (s)" value={String(node.timeout_seconds ?? 120)} />
          </Section>
        )}

        {node.type === "tool" && (
          <Section title="Tool">
            <Row
              label="Binding"
              value={typeof node.tool_name === "string" && node.tool_name ? node.tool_name : "—"}
              mono
            />
            <Row
              label="Binding type"
              value={typeof toolBinding?.type === "string" ? toolBinding.type : "registered_function"}
            />
            {typeof toolBinding?.registry_ref === "string" && (
              <Row label="Registry ref" value={toolBinding.registry_ref} mono />
            )}
            {typeof toolBinding?.version_constraint === "string" && (
              <Row label="Version constraint" value={toolBinding.version_constraint || "—"} mono />
            )}
            {typeof toolBinding?.server_id === "string" && (
              <Row label="Server ID" value={toolBinding.server_id} mono />
            )}
            {typeof toolBinding?.tool_name === "string" && toolBinding.tool_name !== node.tool_name && (
              <Row label="Remote tool" value={toolBinding.tool_name} mono />
            )}
          </Section>
        )}

        {/* ─── MCP Resource ─── */}
        {node.type === "mcp_resource" && (
          <Section title="MCP resource">
            <Row
              label="Server ID"
              value={typeof node.server_id === "string" && node.server_id ? node.server_id : "—"}
              mono
            />
            <Row
              label="Tool"
              value={typeof node.tool_name === "string" && node.tool_name ? node.tool_name : "—"}
              mono
            />
            <Row label="Timeout (s)" value={String(node.timeout_seconds ?? 45)} />
          </Section>
        )}

        {node.type === "knowledge_query" && (
          <Section title="Knowledge retrieval">
            <Row
              label="Knowledge base"
              value={typeof node.knowledge_base_id === "string" && node.knowledge_base_id ? node.knowledge_base_id : "—"}
              mono
            />
            <Row
              label="Pinned versions"
              value={Array.isArray(node.version_ids) && node.version_ids.length > 0 ? node.version_ids.join(", ") : "Active version"}
              mono
            />
            <Row
              label="Modes"
              value={Array.isArray(node.retrieval_modes) && node.retrieval_modes.length > 0 ? node.retrieval_modes.join(", ") : "KB default"}
            />
            <Row label="Top K" value={String(node.top_k ?? 6)} />
            <Row
              label="Chat model"
              value={typeof node.chat_model === "string" && node.chat_model ? node.chat_model : "Workflow default"}
              mono
            />
            <Row
              label="Graph strength"
              value={typeof node.graph_overrides?.retrieval_strength === "string"
                ? node.graph_overrides.retrieval_strength
                : "KB default"}
            />
            <Row
              label="Min relationship weight"
              value={typeof node.graph_overrides?.minimum_relationship_weight === "number"
                ? String(node.graph_overrides.minimum_relationship_weight)
                : "KB default"}
            />
            <Row
              label="AGE seed mode"
              value={typeof node.graph_overrides?.age_seed_mode === "string"
                ? node.graph_overrides.age_seed_mode
                : "KB default"}
            />
            <Row
              label="AGE traversal hops"
              value={typeof node.graph_overrides?.age_traversal_hops === "number"
                ? String(node.graph_overrides.age_traversal_hops)
                : "KB default"}
            />
            <Row
              label="AGE candidate pool"
              value={typeof node.graph_overrides?.age_candidate_pool_size === "number"
                ? String(node.graph_overrides.age_candidate_pool_size)
                : "KB default"}
            />
            <Row
              label="AGE dense rerank"
              value={typeof node.graph_overrides?.age_dense_rerank_weight === "number"
                ? node.graph_overrides.age_dense_rerank_weight.toFixed(2)
                : "KB default"}
            />
            <Row
              label="Strict AGE"
              value={node.graph_overrides?.strict_age_retrieval ? "Enabled" : "Fallback allowed"}
            />
          </Section>
        )}

        {node.type === "knowledge_build" && (
          <Section title="Knowledge build">
            <Row
              label="Knowledge base"
              value={typeof node.knowledge_base_id === "string" && node.knowledge_base_id ? node.knowledge_base_id : "—"}
              mono
            />
            <Row
              label="Chunking strategy"
              value={typeof node.chunking_strategy === "string" && node.chunking_strategy ? node.chunking_strategy : "—"}
            />
            <Row
              label="Embedding model"
              value={typeof node.embedding_model === "string" && node.embedding_model ? node.embedding_model : "—"}
              mono
            />
            <Row
              label="Wait for completion"
              value={node.wait_for_completion ? "Enabled" : "Launch only"}
            />
            <Row
              label="Wait timeout (s)"
              value={String(node.wait_timeout_seconds ?? 300)}
            />
            <Row
              label="Activate when complete"
              value={node.activate_when_complete ? "Enabled" : "Disabled"}
            />
          </Section>
        )}

        {node.type === "template" && (
          <Section title="Template">
            <Row label="Output format" value={node.output_format ?? "text"} />
            <Row label="Missing variables" value={node.missing_variable_mode ?? "preserve"} />
            <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-mono leading-relaxed text-slate-700">
              {typeof node.template === "string" && node.template.trim() ? node.template : "{{input}}"}
            </pre>
          </Section>
        )}

        {node.type === "external_app" && (
          <Section title="External app">
            <Row
              label="Entrypoint"
              value={typeof node.entrypoint === "string" && node.entrypoint ? node.entrypoint : "—"}
              mono
            />
          </Section>
        )}

        {node.type === "python_code" && (
          <Section title="Python code">
            <Row label="Timeout (s)" value={String(node.timeout_seconds ?? 5)} />
            <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-mono leading-relaxed text-slate-700">
              {typeof node.code === "string" && node.code.trim() ? node.code : "# no code configured"}
            </pre>
          </Section>
        )}

        {/* ─── Guardrail ─── */}
        {node.type === "guardrail" && (
          <Section title="Guardrail settings">
            <Row label="Mode" value={node.mode ?? "post_agent"} />
            <Row label="On failure" value={node.on_failure ?? "block"} />
            {node.on_failure === "block_retry" && (
              <Row label="Retry attempts" value={String(node.max_retries ?? 0)} />
            )}
            <Row label="Checks" value={`${node.checks?.length ?? 0} configured`} />
            {node.checks && node.checks.length > 0 && (
              <div className="space-y-1.5 mt-1">
                {node.checks.map((check, i) => (
                  <div key={i} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] font-mono text-slate-600 break-all">
                    {JSON.stringify(check, null, 2)}
                  </div>
                ))}
              </div>
            )}
          </Section>
        )}

        {/* ─── Router ─── */}
        {node.type === "router" && (
          <Section title={`Routing (${node.branches?.length ?? 0} branches)`}>
            {node.branches && node.branches.length > 0 ? (
              <div className="space-y-2">
                {node.branches.map((branch, i) => (
                  <div key={i} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
                    <div className="flex items-center gap-2 text-slate-700">
                      <span className="text-[10px] font-bold uppercase text-amber-600">
                        {branch.condition ? `IF #${i + 1}` : "ELSE"}
                      </span>
                      <span className="text-slate-400">→</span>
                      <span className="font-medium text-slate-900">{branch.to}</span>
                    </div>
                    {branch.condition && (
                      <div className="mt-1 font-mono text-[10px] text-slate-400 break-all">
                        {JSON.stringify(branch.condition)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-slate-400">No branches configured.</div>
            )}
          </Section>
        )}

        {/* ─── Human Approval ─── */}
        {node.type === "human_approval" && (
          <Section title="Human approval">
            <Row
              label="Required role"
              value={typeof node.required_role === "string" && node.required_role.trim()
                ? node.required_role
                : "caliber.approver"}
            />
            <Row
              label="Approval count"
              value={String(
                typeof node.approval_count === "number" && Number.isFinite(node.approval_count)
                  ? node.approval_count
                  : 1,
              )}
            />
            <Row
              label="Timeout behavior"
              value={typeof node.timeout_behavior === "string" ? node.timeout_behavior : "block"}
            />
            <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-3 text-xs text-violet-700">
              ✋ Pauses execution for human approval before continuing to the next step.
            </div>
          </Section>
        )}

        {/* ─── Note ─── */}
        {node.type === "note" && node.text && (
          <Section title="Note">
            <pre className="whitespace-pre-wrap text-xs text-slate-700 bg-slate-50 rounded-lg border border-slate-200 px-3 py-2 leading-relaxed">
              {node.text}
            </pre>
          </Section>
        )}

        {node.execution_policy && (
          <Section title="Execution policy">
            <Row
              label="Timeout (s)"
              value={typeof node.execution_policy.timeout_seconds === "number"
                ? String(node.execution_policy.timeout_seconds)
                : "Runtime default"}
            />
            <Row
              label="Max retries"
              value={String(
                typeof node.execution_policy.max_retries === "number"
                  ? node.execution_policy.max_retries
                  : 0,
              )}
            />
            <Row
              label="Idempotent"
              value={node.execution_policy.idempotent ? "Yes" : "No"}
            />
          </Section>
        )}

        {/* ─── Ports ─── */}
        {(inputPorts.length > 0 || outputPorts.length > 0) && (
          <Section title="Ports">
            {inputPorts.length > 0 && (
              <div className="mb-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 mb-1">
                  Inputs
                </div>
                <div className="space-y-1">
                  {inputPorts.map(([name, spec]) => (
                    <div
                      key={name}
                      className="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs"
                    >
                      <span
                        className="h-2.5 w-2.5 shrink-0 rounded-full"
                        style={{ backgroundColor: portColor(spec.type) }}
                      />
                      <span className="font-mono font-medium text-slate-800">{name}</span>
                      <span className="ml-auto text-[10px] text-slate-400">{spec.type}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {outputPorts.length > 0 && (
              <div>
                <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 mb-1">
                  Outputs
                </div>
                <div className="space-y-1">
                  {outputPorts.map(([name, spec]) => (
                    <div
                      key={name}
                      className="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs"
                    >
                      <span
                        className="h-2.5 w-2.5 shrink-0 rounded-full"
                        style={{ backgroundColor: portColor(spec.type) }}
                      />
                      <span className="font-mono font-medium text-slate-800">{name}</span>
                      <span className="ml-auto text-[10px] text-slate-400">{spec.type}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Section>
        )}

        {/* ─── Connections ─── */}
        {(incomingEdges.length > 0 || outgoingEdges.length > 0) && (
          <Section title="Connections">
            {incomingEdges.length > 0 && (
              <div className="mb-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 mb-1">
                  From ({incomingEdges.length})
                </div>
                <div className="space-y-1">
                  {incomingEdges.map((e) => {
                    const sourceNode = manifest.nodes[e.from];
                    return (
                      <div key={e.id} className="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs">
                        <span className="text-slate-400">←</span>
                        <span className="font-medium text-slate-800">
                          {sourceNode ? nodeLabel(sourceNode) : e.from}
                        </span>
                        {Object.keys(e.map).length > 0 && (
                          <span className="ml-auto text-[10px] text-slate-400">
                            {Object.keys(e.map).length} mapping{Object.keys(e.map).length !== 1 ? "s" : ""}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {outgoingEdges.length > 0 && (
              <div>
                <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 mb-1">
                  To ({outgoingEdges.length})
                </div>
                <div className="space-y-1">
                  {outgoingEdges.map((e) => {
                    const targetNode = manifest.nodes[e.to];
                    return (
                      <div key={e.id} className="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs">
                        <span className="text-slate-400">→</span>
                        <span className="font-medium text-slate-800">
                          {targetNode ? nodeLabel(targetNode) : e.to}
                        </span>
                        {Object.keys(e.map).length > 0 && (
                          <span className="ml-auto text-[10px] text-slate-400">
                            {Object.keys(e.map).length} mapping{Object.keys(e.map).length !== 1 ? "s" : ""}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </Section>
        )}

        {/* ─── Raw properties (catch-all for unknown fields) ─── */}
        <RawProperties node={node} />
      </div>
    </div>
  );
}

/** Shows any node properties not covered by the typed sections above. */
function RawProperties({ node }: { node: ManifestNode }): JSX.Element | null {
  const KNOWN_KEYS = new Set([
    "id", "type", "name", "model", "instructions", "tools", "skills",
    "tool_constraints", "handoffs", "inputs", "outputs", "output_type",
    "eval_dataset", "trigger", "path", "max_bytes", "encoding", "pattern",
    "recursive", "max_files", "max_bytes_per_file", "bucket", "prefix",
    "overwrite", "wait_until", "timezone", "event_name", "correlation_key",
    "timeout_seconds", "mode", "target_node_id", "item_input_port", "max_items",
    "max_iterations", "stop_condition",
    "fallback_text", "compensate_with", "workflow_id", "alias", "server_id",
    "tool_name", "knowledge_base_id", "version_ids", "retrieval_modes",
    "top_k", "chat_model", "graph_overrides", "entrypoint", "code", "checks",
    "on_failure", "max_retries", "branches", "required_role", "approval_count",
    "timeout_behavior", "text", "execution_policy",
  ]);
  const extra = Object.entries(node).filter(
    ([k, v]) => !KNOWN_KEYS.has(k) && v !== undefined && v !== null,
  );
  if (extra.length === 0) return null;

  return (
    <Section title="Additional properties">
      <div className="space-y-1.5">
        {extra.map(([key, value]) => (
          <div key={key} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 mb-0.5">
              {key}
            </div>
            <div className="text-xs font-mono text-slate-600 break-all">
              {typeof value === "object" ? JSON.stringify(value, null, 2) : String(value)}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }): JSX.Element {
  return (
    <div className="space-y-2">
      <h4 className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
        {title}
      </h4>
      {children}
    </div>
  );
}

function Row({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): JSX.Element {
  return (
    <div className="flex items-baseline justify-between gap-4 text-xs">
      <span className="text-slate-500 shrink-0">{label}</span>
      <span
        className={`text-right text-slate-800 truncate ${mono ? "font-mono text-[11px]" : ""}`}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}
