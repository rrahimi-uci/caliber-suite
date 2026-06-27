/**
 * Pure helpers that bridge a CALIBER workflow manifest and a React Flow graph.
 *
 * The manifest is the source of truth; the editor renders it as a graph and
 * edits flow back into the manifest object. Layout is a simple left-to-right
 * layered placement computed by BFS depth from the start node — deterministic
 * and dependency-free (no dagre), which keeps these functions trivially
 * testable and avoids a heavy layout dependency for the MVP.
 */

import type { Edge, Node } from "@xyflow/react";

import type { KnowledgeRetrievalMode } from "@/api/knowledgeTypes";
import type {
  ManifestEdge,
  ManifestNode,
  McpServer,
  PortSpec,
  ToolDefinition,
  ValidationIssue,
  ValidationReport,
  WorkflowComponent,
  WorkflowComponentSetupCheck,
  WorkflowManifest,
  WorkflowNodeType,
  WorkflowRuntimeConfig,
  WorkflowTemplateKind,
  WorkflowToolBinding,
} from "@/api/workflowTypes";

export interface FlowNodeData extends Record<string, unknown> {
  node: ManifestNode;
  label: string;
  componentSpec?: WorkflowComponent | null;
  manifest?: WorkflowManifest | null;
  validationSummary?: NodeValidationSummary;
  executionBadge?: NodeExecutionBadge;
}

export interface FlowNodePosition {
  x: number;
  y: number;
}

export interface NodeGuideCheck {
  label: string;
  help: string;
  satisfied: boolean;
}

export interface NodeGuide {
  summary: string;
  tips: string[];
  checks: NodeGuideCheck[];
  missingLabels: string[];
}

export interface NodeValidationSummary {
  severity: "error" | "warning" | "setup" | "ok";
  errors: number;
  warnings: number;
  missingLabels: string[];
  title: string;
}

export interface NodeExecutionBadge {
  status: string;
  label: string;
  source: "preview" | "run";
  tone: "success" | "info" | "warning" | "error" | "neutral";
  current: boolean;
}

export interface NodePaletteItem {
  type: WorkflowNodeType;
  label: string;
  group: string;
  description: string;
  docs?: string[];
  fieldCount?: number;
  setupRuleCount?: number;
  defaultInputCount?: number;
  defaultOutputCount?: number;
  legacy?: boolean;
  legacyReplacement?: string | null;
}

interface NodeGuideCheckDefinition {
  label: string;
  help: string;
  test: (node: ManifestNode, manifest?: WorkflowManifest | null) => boolean;
  when?: (node: ManifestNode, manifest?: WorkflowManifest | null) => boolean;
}

interface NodeGuideDefinition {
  summary: string;
  tips?: string[];
  checks?: NodeGuideCheckDefinition[];
}

const COLUMN_WIDTH = 320;
const ROW_HEIGHT = 140;

function hasText(value: unknown): boolean {
  return typeof value === "string" && value.trim().length > 0;
}

function hasItems(value: unknown): boolean {
  return Array.isArray(value) && value.length > 0;
}

const EXECUTABLE_ORCHESTRATION_TARGET_TYPES = new Set<WorkflowNodeType>([
  "agent",
  "subworkflow",
  "tool",
  "mcp_resource",
  "knowledge_query",
  "knowledge_build",
  "template",
  "python_code",
  "external_app",
]);

function nodeEdgesFrom(
  manifest: WorkflowManifest | null | undefined,
  nodeId: string,
): ManifestEdge[] {
  if (!manifest) return [];
  return manifest.edges.filter((edge) => edge.from === nodeId);
}

function nodeEdgesTo(
  manifest: WorkflowManifest | null | undefined,
  nodeId: string,
): ManifestEdge[] {
  if (!manifest) return [];
  return manifest.edges.filter((edge) => edge.to === nodeId);
}

function hasOutgoingEdgeToTarget(
  manifest: WorkflowManifest | null | undefined,
  fromNodeId: string,
  toNodeId: string,
): boolean {
  return nodeEdgesFrom(manifest, fromNodeId).some((edge) => edge.to === toNodeId);
}

function hasDistinctIncomingTargetPorts(
  manifest: WorkflowManifest | null | undefined,
  nodeId: string,
): boolean {
  const ports = new Set<string>();
  for (const edge of nodeEdgesTo(manifest, nodeId)) {
    for (const targetPort of Object.values(edge.map ?? {})) {
      const normalized =
        typeof targetPort === "string" ? targetPort.trim() : "";
      if (!normalized) continue;
      if (ports.has(normalized)) return false;
      ports.add(normalized);
    }
  }
  return true;
}

function manifestNodeById(
  manifest: WorkflowManifest | null | undefined,
  nodeId: string | null | undefined,
): ManifestNode | null {
  if (!manifest || typeof nodeId !== "string" || nodeId.trim().length === 0) {
    return null;
  }
  return manifest.nodes[nodeId] ?? null;
}

function isExecutableOrchestrationTarget(
  node: ManifestNode | null | undefined,
): boolean {
  return Boolean(
    node && EXECUTABLE_ORCHESTRATION_TARGET_TYPES.has(node.type),
  );
}

function incomingMappedPorts(
  manifest: WorkflowManifest | null | undefined,
  nodeId: string,
): Set<string> {
  const ports = new Set<string>();
  if (!manifest) return ports;
  for (const edge of manifest.edges) {
    if (edge.to !== nodeId) continue;
    for (const targetPort of Object.values(edge.map ?? {})) {
      if (typeof targetPort === "string" && targetPort.trim()) {
        ports.add(targetPort.trim());
      }
    }
  }
  return ports;
}

function satisfiesMappedInputSetup(
  node: ManifestNode,
  field: string | null | undefined,
  manifest: WorkflowManifest | null | undefined,
): boolean {
  if (!field) return false;
  const mappedPorts = incomingMappedPorts(manifest, node.id);
  if (!mappedPorts.has(field)) return false;
  const inputs = node.inputs;
  return Boolean(inputs && typeof inputs === "object" && field in inputs);
}

function nodeFieldValue(
  node: ManifestNode,
  field: string | null | undefined,
): unknown {
  if (!field) return undefined;
  return (node as Record<string, unknown>)[field];
}

function hasInstructions(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const instructions = value as Record<string, unknown>;
  if (instructions.type === "inline") return hasText(instructions.text);
  if (instructions.type === "mlflow_prompt") return hasText(instructions.ref);
  return false;
}

function readTextValue(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function evaluateComponentSetupCheck(
  node: ManifestNode,
  check: WorkflowComponentSetupCheck,
  manifest: WorkflowManifest | null | undefined = null,
): boolean {
  switch (check.kind) {
    case "non_empty_string":
      return (
        hasText(nodeFieldValue(node, check.field)) ||
        satisfiesMappedInputSetup(node, check.field, manifest)
      );
    case "non_empty_list":
      return hasItems(nodeFieldValue(node, check.field));
    case "any_non_empty": {
      const fields = check.fields?.length
        ? check.fields
        : check.field
          ? [check.field]
          : [];
      return fields.some((field) => {
        const value = nodeFieldValue(node, field);
        return (
          hasText(value) ||
          hasItems(value) ||
          satisfiesMappedInputSetup(node, field, manifest)
        );
      });
    }
    case "instructions_present":
      return hasInstructions(node.instructions);
    case "minimum_number": {
      const value = nodeFieldValue(node, check.field);
      if (typeof value !== "number" || !Number.isFinite(value)) return false;
      return value >= (check.minimum ?? 0);
    }
    case "minimum_outgoing_edges":
      if (!manifest) return true;
      return nodeEdgesFrom(manifest, node.id).length >= (check.minimum ?? 0);
    case "minimum_incoming_edges":
      if (!manifest) return true;
      return nodeEdgesTo(manifest, node.id).length >= (check.minimum ?? 0);
    case "distinct_incoming_target_ports":
      if (!manifest) return true;
      return hasDistinctIncomingTargetPorts(manifest, node.id);
    case "target_node_executable_if_set": {
      const targetNodeId = readTextValue(nodeFieldValue(node, check.field));
      if (!targetNodeId || !manifest) return true;
      return isExecutableOrchestrationTarget(
        manifestNodeById(manifest, targetNodeId),
      );
    }
    case "not_current_workflow_id": {
      const workflowId = readTextValue(nodeFieldValue(node, check.field));
      if (!workflowId || !manifest) return true;
      return manifest.workflow_id.trim() !== workflowId.trim();
    }
    case "router_branch_edges_connected": {
      const branches = Array.isArray(node.branches) ? node.branches : [];
      if (branches.length === 0 || !manifest) return true;
      return branches.every(
        (branch) =>
          hasText(branch.to) &&
          Boolean(manifest.nodes[branch.to]) &&
          hasOutgoingEdgeToTarget(manifest, node.id, branch.to),
      );
    }
    default:
      return false;
  }
}

function componentSetupCheckTouchesField(
  check: WorkflowComponentSetupCheck,
  fieldKey: string,
): boolean {
  if (check.field === fieldKey) return true;
  return Array.isArray(check.fields) && check.fields.includes(fieldKey);
}

function nodeFieldPathPrefix(nodeId: string, fieldKey: string): string {
  return `nodes.${nodeId}.${fieldKey}`;
}

function matchesNodeFieldPath(path: string, prefix: string): boolean {
  return (
    path === prefix ||
    path.startsWith(`${prefix}.`) ||
    path.startsWith(`${prefix}[`)
  );
}

function nodeExecutionTone(status: string): NodeExecutionBadge["tone"] {
  switch (status) {
    case "ok":
    case "completed":
      return "success";
    case "running":
    case "resuming":
    case "queued":
    case "waiting_approval":
      return "info";
    case "waiting_event":
    case "blocked":
      return "warning";
    case "failed":
    case "error":
    case "rejected":
      return "error";
    case "cancelled":
    case "expired":
    case "skipped":
    default:
      return "neutral";
  }
}

function nodeExecutionLabel(status: string): string {
  switch (status) {
    case "waiting_approval":
      return "approval";
    case "waiting_event":
      return "event";
    case "completed":
      return "done";
    case "failed":
    case "error":
      return "error";
    default:
      return status.replaceAll("_", " ");
  }
}

function makeNodeExecutionBadge(
  status: string,
  source: NodeExecutionBadge["source"],
  options: { current?: boolean } = {},
): NodeExecutionBadge {
  return {
    status,
    label: nodeExecutionLabel(status),
    source,
    tone: nodeExecutionTone(status),
    current: options.current === true,
  };
}

export function nodeExecutionColor(status: string): string {
  switch (status) {
    case "ok":
    case "completed":
      return "#22C55E";
    case "running":
    case "resuming":
      return "#0EA5E9";
    case "queued":
    case "waiting_approval":
      return "#8B5CF6";
    case "waiting_event":
    case "blocked":
      return "#F59E0B";
    case "failed":
    case "error":
    case "rejected":
      return "#EF4444";
    case "cancelled":
    case "expired":
    case "skipped":
      return "#71717A";
    default:
      return "#6B7280";
  }
}

export function buildNodeExecutionBadgeMap({
  previewSteps = [],
  runSteps = [],
  runStatus = null,
  currentNodeId = null,
}: {
  previewSteps?: ReadonlyArray<{ node_id?: unknown; status?: unknown }>;
  runSteps?: ReadonlyArray<{ node_id?: unknown; status?: unknown }>;
  runStatus?: string | null;
  currentNodeId?: string | null;
}): Record<string, NodeExecutionBadge> {
  const badges: Record<string, NodeExecutionBadge> = {};

  for (const step of previewSteps) {
    if (typeof step.node_id !== "string" || !step.node_id.trim()) continue;
    const status =
      typeof step.status === "string" && step.status.trim()
        ? step.status
        : "ok";
    badges[step.node_id] = makeNodeExecutionBadge(status, "preview");
  }

  let lastRunNodeId: string | null = null;
  for (const step of runSteps) {
    if (typeof step.node_id !== "string" || !step.node_id.trim()) continue;
    const status =
      typeof step.status === "string" && step.status.trim()
        ? step.status
        : "ok";
    badges[step.node_id] = makeNodeExecutionBadge(status, "run");
    lastRunNodeId = step.node_id;
  }

  if (
    currentNodeId &&
    [
      "queued",
      "running",
      "resuming",
      "waiting_approval",
      "waiting_event",
    ].includes(runStatus ?? "")
  ) {
    badges[currentNodeId] = makeNodeExecutionBadge(
      runStatus ?? "running",
      "run",
      { current: true },
    );
    return badges;
  }

  if (
    ["failed", "cancelled", "rejected", "expired", "blocked"].includes(
      runStatus ?? "",
    )
  ) {
    const terminalNodeId = currentNodeId || lastRunNodeId;
    if (terminalNodeId) {
      badges[terminalNodeId] = makeNodeExecutionBadge(
        runStatus ?? "failed",
        "run",
      );
    }
  }

  return badges;
}

const NODE_GUIDANCE: Record<WorkflowNodeType, NodeGuideDefinition> = {
  start: {
    summary:
      "Defines how the workflow begins, whether manually, on a schedule, or from an event trigger.",
    tips: [
      "Switch to event or cron mode when runs should start automatically.",
    ],
  },
  file_input: {
    summary:
      "Loads one local file into the workflow context so downstream nodes can read its contents.",
    tips: [
      "Use this for a single known file path or when an operator provides the file at run time.",
    ],
    checks: [
      {
        label: "Provide a file path",
        help: "Set the file path directly or map one into the node's path input.",
        test: (node, manifest) =>
          hasText(node.path) ||
          satisfiesMappedInputSetup(node, "path", manifest),
      },
    ],
  },
  folder_input: {
    summary:
      "Reads matching files from a local folder and emits them as workflow inputs.",
    tips: [
      "Use patterns to bound the file set before sending content into agents or code nodes.",
    ],
    checks: [
      {
        label: "Provide a folder path",
        help: "Set the folder path directly or map one into the node's path input.",
        test: (node, manifest) =>
          hasText(node.path) ||
          satisfiesMappedInputSetup(node, "path", manifest),
      },
    ],
  },
  input_bucket: {
    summary:
      "Reads bounded text objects from the object store and preserves per-object source metadata for downstream workflow steps.",
    tips: [
      "Pair this with prefixes and max object limits so runs stay bounded and predictable.",
      "Unreadable objects are skipped and surfaced in metadata, while true bucket-list failures stop the run.",
    ],
    checks: [
      {
        label: "Select an input bucket",
        help: "Choose the object-store bucket this node should scan. Unreadable objects are skipped and surfaced in metadata; bucket-list failures stop the run.",
        test: (node) => hasText(node.bucket),
      },
    ],
  },
  output_bucket: {
    summary:
      "Writes workflow artifacts back to object storage for downstream systems or human review.",
    tips: [
      "Use prefixes to keep run outputs grouped and easy to inspect in Object Storage.",
    ],
    checks: [
      {
        label: "Select an output bucket",
        help: "Choose where artifacts should be written.",
        test: (node) => hasText(node.bucket),
      },
    ],
  },
  output_folder: {
    summary: "Writes workflow artifacts to a local filesystem directory.",
    tips: [
      "Use this when another local process or export step needs files on disk.",
    ],
    checks: [
      {
        label: "Provide an output folder path",
        help: "Choose the destination directory or map one into the node's path input.",
        test: (node, manifest) =>
          hasText(node.path) ||
          satisfiesMappedInputSetup(node, "path", manifest),
      },
    ],
  },
  wait_until: {
    summary: "Pauses execution until a specific timestamp before continuing.",
    tips: [
      "Useful for delayed follow-ups, scheduled retries, and time-based coordination.",
    ],
    checks: [
      {
        label: "Set the target timestamp",
        help: "Choose the time the workflow should resume.",
        test: (node) => hasText(node.wait_until),
      },
    ],
  },
  wait_for_event: {
    summary: "Suspends the run until an external event payload resumes it.",
    tips: [
      "Use this for approval callbacks, external system acknowledgements, or webhook-driven continuation.",
    ],
    checks: [
      {
        label: "Name the resume event",
        help: "Set the event name operators or systems will use to resume this run.",
        test: (node) => hasText(node.event_name),
      },
    ],
  },
  parallel: {
    summary:
      "Fans out execution so multiple downstream branches can work from the same upstream result.",
    tips: [
      "Pair with Join when several branches must converge before the workflow can continue.",
    ],
    checks: [
      {
        label: "Add at least two downstream branches",
        help: "Connect this parallel node to at least two downstream branches before using it as a fan-out barrier.",
        when: (_node, manifest) => Boolean(manifest),
        test: (node, manifest) => nodeEdgesFrom(manifest, node.id).length >= 2,
      },
    ],
  },
  join: {
    summary: "Merges parallel branches into one continuation point.",
    tips: [
      "Use mode 'all' when every branch must finish, or 'any' when the first useful result wins.",
    ],
    checks: [
      {
        label: "Connect at least two upstream branches",
        help: "Feed this join from at least two upstream branches, or remove the join barrier.",
        when: (_node, manifest) => Boolean(manifest),
        test: (node, manifest) => nodeEdgesTo(manifest, node.id).length >= 2,
      },
      {
        label: "Use distinct join input ports per branch",
        help: "Map each incoming branch into a distinct join input port so the merge stays traceable.",
        when: (_node, manifest) => Boolean(manifest),
        test: (node, manifest) =>
          hasDistinctIncomingTargetPorts(manifest, node.id),
      },
    ],
  },
  for_each: {
    summary:
      "Iterates over a collection and can optionally invoke a target node for each item.",
    tips: [
      "Great for batch processing documents, records, retrieved chunks, or templated payloads one item at a time.",
    ],
    checks: [
      {
        label: "Use an executable target when set",
        help: "If you choose a target node for this loop, it must point to an executable step.",
        when: (node, manifest) => Boolean(manifest) && hasText(node.target_node_id),
        test: (node, manifest) =>
          isExecutableOrchestrationTarget(
            manifestNodeById(manifest, node.target_node_id),
          ),
      },
    ],
  },
  loop: {
    summary:
      "Repeats one executable target until a stop condition matches or the loop reaches its maximum iteration cap.",
    tips: [
      "Use this for bounded refinement, retry-until-success patterns, or agent/tool loops that must stay deterministic and inspectable.",
      "Stop conditions can reference iteration, state, output, result, and outputs after each pass.",
    ],
    checks: [
      {
        label: "Select a loop target",
        help: "Choose the executable node this loop should repeat.",
        test: (node) => hasText(node.target_node_id),
      },
      {
        label: "Choose an executable loop target",
        help: "The selected loop target should point to an executable node in this workflow.",
        when: (node, manifest) => Boolean(manifest) && hasText(node.target_node_id),
        test: (node, manifest) =>
          isExecutableOrchestrationTarget(
            manifestNodeById(manifest, node.target_node_id),
          ),
      },
    ],
  },
  error_boundary: {
    summary:
      "Wraps a risky step with fallback handling so one failure does not collapse the entire workflow.",
    tips: [
      "Use fallback text or a compensating node when the workflow should degrade gracefully on failure.",
    ],
    checks: [
      {
        label: "Protect an executable target when set",
        help: "If this boundary wraps a target node, that target should be an executable step.",
        when: (node, manifest) => Boolean(manifest) && hasText(node.target_node_id),
        test: (node, manifest) =>
          isExecutableOrchestrationTarget(
            manifestNodeById(manifest, node.target_node_id),
          ),
      },
      {
        label: "Use an executable compensation node when set",
        help: "If you configure a compensation node, it should point to an executable recovery step.",
        when: (node, manifest) => Boolean(manifest) && hasText(node.compensate_with),
        test: (node, manifest) =>
          isExecutableOrchestrationTarget(
            manifestNodeById(manifest, node.compensate_with),
          ),
      },
    ],
  },
  subworkflow: {
    summary: "Calls another workflow as a nested reusable step.",
    tips: [
      "Use this to share reviewed logic across teams without duplicating large graphs.",
    ],
    checks: [
      {
        label: "Select the workflow to invoke",
        help: "Choose the child workflow this node should run.",
        test: (node) => hasText(node.workflow_id),
      },
      {
        label: "Avoid calling this workflow recursively",
        help: "Choose a different published child workflow instead of pointing this node back at the current workflow.",
        when: (node, manifest) => Boolean(manifest) && hasText(node.workflow_id),
        test: (node, manifest) =>
          manifest?.workflow_id.trim() !== node.workflow_id?.trim(),
      },
    ],
  },
  tool: {
    summary:
      "Invokes a registered tool binding directly from the workflow runtime.",
    tips: [
      "Use this when the workflow should call a capability deterministically without asking an LLM to decide.",
      "Tool nodes reuse the same manifest bindings, preview rules, retries, and MCP-backed integrations that agent tools use.",
    ],
    checks: [
      {
        label: "Select a tool binding",
        help: "Choose the manifest tool binding this node should invoke directly.",
        test: (node) => hasText(node.tool_name),
      },
    ],
  },
  mcp_resource: {
    summary:
      "Invokes a tool exposed by an MCP server from inside the workflow runtime.",
    tips: [
      "Use MCP resources when the capability already exists behind a managed MCP server integration.",
    ],
    checks: [
      {
        label: "Select an MCP server",
        help: "Choose the active MCP server hosting the tool.",
        test: (node) => hasText(node.server_id),
      },
      {
        label: "Select an MCP tool",
        help: "Pick the tool to call on that server.",
        test: (node) => hasText(node.tool_name),
      },
    ],
  },
  webhook: {
    summary:
      "Sends an outbound HTTP request to an external URL and publishes the response downstream.",
    tips: [
      "Use it to notify external systems or call simple REST endpoints without writing a tool.",
      "Reference auth secrets by name in headers rather than pasting them inline.",
    ],
    checks: [
      {
        label: "Provide a request URL",
        help: "Set the HTTP(S) endpoint this webhook should call.",
        test: (node) => hasText(node.url),
      },
    ],
  },
  api_request: {
    summary: "Make HTTP requests using a URL + method or a pasted cURL command.",
    tips: [
      "URL mode builds the request from the URL, method, headers, and body; cURL mode parses a pasted command.",
      "Leave the body empty to send the upstream payload (or input) as the request body.",
    ],
    checks: [
      {
        label: "Provide a URL or cURL command",
        help: "Set a request URL (URL mode) or paste a cURL command (cURL mode).",
        test: (node) =>
          node.mode === "curl" ? hasText(node.curl) : hasText(node.url),
      },
    ],
  },
  knowledge_query: {
    summary:
      "Queries a knowledge base to answer questions with dense, hybrid, or AGE graph retrieval.",
    tips: [
      "Leave pinned versions empty when the workflow should follow the KB's active version at runtime.",
      "Leave retrieval modes empty to follow the knowledge base's default retrieval policy at runtime.",
      "Enable AGE retrieval when graph traversal should influence which chunks and relationships are returned.",
    ],
    checks: [
      {
        label: "Select a knowledge base or pinned versions",
        help: "Choose the target knowledge base or pin explicit KB versions for this query.",
        test: (node, manifest) =>
          hasText(node.knowledge_base_id) ||
          hasItems(node.version_ids) ||
          satisfiesMappedInputSetup(node, "version_ids", manifest),
      },
    ],
  },
  knowledge_build: {
    summary:
      "Launches a new knowledge-base version build so workflows can refresh chunking, embeddings, and graph artifacts.",
    tips: [
      "Leave sources and graph config unwired to reuse the knowledge base's saved source manifest and latest graph profile.",
      "Turn on wait for completion only when downstream nodes need the new version to be finished before they continue.",
    ],
    checks: [
      {
        label: "Select a knowledge base",
        help: "Choose the existing knowledge base this node should refresh.",
        test: (node) => hasText(node.knowledge_base_id),
      },
      {
        label: "Choose a chunking strategy",
        help: "Set the chunker directly or map one into the chunking_strategy input.",
        test: (node, manifest) =>
          hasText(node.chunking_strategy) ||
          satisfiesMappedInputSetup(node, "chunking_strategy", manifest),
      },
      {
        label: "Choose an embedding model",
        help: "Set the embedding model directly or map one into the embedding_model input.",
        test: (node, manifest) =>
          hasText(node.embedding_model) ||
          satisfiesMappedInputSetup(node, "embedding_model", manifest),
      },
    ],
  },
  template: {
    summary:
      "Renders a no-code text prompt or JSON payload from workflow inputs and variables.",
    tips: [
      "Use `{{input}}`, `{{variables.customer.name}}`, or `{{items[0]}}` placeholders to shape downstream prompts and tool payloads.",
      "Switch to JSON mode when downstream nodes need a parsed object instead of plain text.",
    ],
    checks: [
      {
        label: "Provide a template",
        help: "Write the text or JSON template this node should render.",
        test: (node) => hasText(node.template),
      },
    ],
  },
  python_code: {
    summary:
      "Runs custom Python inside the workflow sandbox for bespoke transformations or control logic.",
    tips: [
      "Keep code focused on deterministic transforms; use agents or tools for networked reasoning.",
    ],
    checks: [
      {
        label: "Provide Python code",
        help: "Write or paste the code this node should execute.",
        test: (node) => hasText(node.code),
      },
    ],
  },
  agent: {
    summary:
      "Runs an LLM-powered step with optional tools, skills, and handoffs.",
    tips: [
      "Use tools for grounding, guardrails for policy, and handoffs for specialist agents.",
    ],
    checks: [
      {
        label: "Provide instructions or a prompt reference",
        help: "Set inline instructions or bind the agent to a registered prompt.",
        test: (node) => {
          const instructions = node.instructions;
          if (!instructions) return false;
          if (instructions.type === "inline") return hasText(instructions.text);
          return hasText(instructions.ref);
        },
      },
    ],
  },
  guardrail: {
    summary:
      "Applies policy, safety, or structural checks before or after an agent step.",
    tips: [
      "Use guardrails to block, redact, or escalate risky outputs before they reach the user.",
    ],
    checks: [
      {
        label: "Configure at least one guardrail check",
        help: "Choose the checks this node should apply to the response or input.",
        test: (node) => hasItems(node.checks),
      },
    ],
  },
  router: {
    summary:
      "Chooses downstream branches based on structured routing conditions.",
    tips: [
      "Pair routers with deterministic outputs or structured response fields so branch selection stays reliable.",
    ],
    checks: [
      {
        label: "Add at least one branch",
        help: "Define the branch destinations and routing conditions.",
        test: (node) => hasItems(node.branches),
      },
      {
        label: "Connect every branch target with an outgoing edge",
        help: "Each configured branch should point to a real node and also have a matching outgoing edge from this router.",
        when: (node, manifest) => Boolean(manifest) && hasItems(node.branches),
        test: (node, manifest) =>
          (node.branches ?? []).every(
            (branch) =>
              hasText(branch.to) &&
              Boolean(manifest?.nodes[branch.to]) &&
              hasOutgoingEdgeToTarget(manifest, node.id, branch.to),
          ),
      },
    ],
  },
  human_approval: {
    summary:
      "Pauses a workflow for human review until the required approval decision has been recorded.",
    tips: [
      "Place this after risky tools, expensive actions, or customer-facing responses that need approval.",
      "Increase the approval count when sensitive actions require more than one reviewer before the run can resume.",
    ],
    checks: [
      {
        label: "Set the reviewer scope",
        help: "Choose which CALIBER scope can approve or reject this gate.",
        test: (node) =>
          hasText(
            (node as Record<string, unknown>).required_role ??
              "caliber.approver",
          ),
      },
      {
        label: "Require at least one approval",
        help: "Set how many approval decisions the run must collect before it can resume.",
        test: (node) =>
          typeof (node as Record<string, unknown>).approval_count !==
            "number" ||
          Number((node as Record<string, unknown>).approval_count) >= 1,
      },
    ],
  },
  output: {
    summary:
      "Ends the workflow and publishes the final response or artifact references.",
    tips: [
      "Make sure the right upstream port is mapped into response so operators see meaningful run output.",
    ],
  },
  note: {
    summary:
      "Adds documentation or design intent directly onto the canvas without affecting execution.",
    tips: [
      "Use notes to explain assumptions, owner expectations, or why a branch exists.",
    ],
  },
  external_app: {
    summary:
      "Bridges a prebuilt external app into the workflow graph as a migration-friendly step.",
    tips: [
      "Use this while moving existing code into first-class workflow nodes over time.",
    ],
    checks: [
      {
        label: "Set the external app entrypoint",
        help: "Provide the app entrypoint the runtime should invoke.",
        test: (node) => hasText((node as Record<string, unknown>).entrypoint),
      },
    ],
  },
};

/** Human label for a node (agent name, or the node id). */
export function nodeLabel(node: ManifestNode): string {
  // A custom display label wins; then an agent's name; otherwise the node id.
  if (typeof node.label === "string" && node.label.trim()) return node.label.trim();
  if (node.type === "agent" && typeof node.name === "string") return node.name;
  return node.id;
}

/** Short summary line shown under the node title. */
export function nodeSubtitle(node: ManifestNode): string {
  switch (node.type) {
    case "agent": {
      const tools = node.tools?.length ?? 0;
      const model = typeof node.model === "string" ? node.model : "inherit";
      return `${model} · ${tools} tool${tools === 1 ? "" : "s"}`;
    }
    case "guardrail":
      return `${node.mode ?? "post_agent"} · ${node.checks?.length ?? 0} check(s)`;
    case "file_input":
      return `${typeof node.path === "string" && node.path ? node.path : "path input"} · file`;
    case "folder_input": {
      const pattern =
        typeof node.pattern === "string" && node.pattern
          ? node.pattern
          : "**/*";
      return `${pattern} · ${node.max_files ?? 50} files`;
    }
    case "input_bucket": {
      const bucket = node.bucket ? node.bucket : "(no bucket)";
      const prefix = node.prefix ? `/${node.prefix}` : "";
      return `${bucket}${prefix} · ${node.max_files ?? 50} objects`;
    }
    case "output_bucket": {
      const bucket = node.bucket ? node.bucket : "(no bucket)";
      const prefix = node.prefix ? `/${node.prefix}` : "";
      return `${bucket}${prefix} · artifacts`;
    }
    case "output_folder":
      return `${node.path ? node.path : "(no path)"} · artifacts`;
    case "wait_until":
      return `${typeof node.wait_until === "string" ? node.wait_until : "until time"} · wait`;
    case "wait_for_event":
      return `${typeof node.event_name === "string" && node.event_name ? node.event_name : "event"} · wait`;
    case "parallel":
      return "fan-out";
    case "join":
      return "fan-in barrier";
    case "for_each":
      return `${node.max_items ?? 100} max items`;
    case "loop":
      return `${node.max_iterations ?? 10} max iterations`;
    case "error_boundary":
      return `${typeof node.target_node_id === "string" && node.target_node_id ? node.target_node_id : "target"} · guarded`;
    case "subworkflow":
      return `${typeof node.workflow_id === "string" && node.workflow_id ? node.workflow_id : "workflow"}@${typeof node.alias === "string" && node.alias ? node.alias : "prod"}`;
    case "tool":
      return `${typeof node.tool_name === "string" && node.tool_name ? node.tool_name : "tool"} · direct`;
    case "mcp_resource": {
      const server =
        typeof node.server_id === "string" && node.server_id
          ? node.server_id
          : "server";
      const tool =
        typeof node.tool_name === "string" && node.tool_name
          ? node.tool_name
          : "tool";
      return `${server} · ${tool}`;
    }
    case "webhook": {
      const method = typeof node.method === "string" && node.method ? node.method : "POST";
      const url = typeof node.url === "string" && node.url ? node.url : "(no url)";
      return `${method} · ${url}`;
    }
    case "api_request": {
      if (node.mode === "curl") return "cURL request";
      const method = typeof node.method === "string" && node.method ? node.method : "GET";
      const url = typeof node.url === "string" && node.url ? node.url : "(no url)";
      return `${method} · ${url}`;
    }
    case "knowledge_query": {
      const kb =
        typeof node.knowledge_base_id === "string" && node.knowledge_base_id
          ? node.knowledge_base_id
          : "knowledge base";
      const modes =
        Array.isArray(node.retrieval_modes) && node.retrieval_modes.length > 0
          ? node.retrieval_modes.join(" + ")
          : "KB default";
      return `${kb} · ${modes}`;
    }
    case "knowledge_build": {
      const kb =
        typeof node.knowledge_base_id === "string" && node.knowledge_base_id
          ? node.knowledge_base_id
          : "knowledge base";
      const chunker =
        typeof node.chunking_strategy === "string" && node.chunking_strategy
          ? node.chunking_strategy
          : "chunker";
      const embedder =
        typeof node.embedding_model === "string" && node.embedding_model
          ? node.embedding_model
          : "embedder";
      return `${kb} · ${chunker} · ${embedder}`;
    }
    case "template": {
      const outputFormat = node.output_format ?? "text";
      const missingMode = node.missing_variable_mode ?? "preserve";
      return `${outputFormat} template · missing ${missingMode}`;
    }
    case "external_app":
      return `${typeof node.entrypoint === "string" && node.entrypoint ? node.entrypoint : "entrypoint"} · bridge`;
    case "python_code":
      return `${node.timeout_seconds ?? 5}s timeout · sandboxed`;
    case "router":
      return `${node.branches?.length ?? 0} branch(es)`;
    case "human_approval": {
      const role =
        typeof node.required_role === "string" && node.required_role.trim()
          ? node.required_role.trim()
          : "caliber.approver";
      const approvals =
        typeof node.approval_count === "number" &&
        Number.isFinite(node.approval_count)
          ? node.approval_count
          : 1;
      return `${approvals} approval${approvals === 1 ? "" : "s"} · ${role}`;
    }
    default:
      return node.type;
  }
}

/**
 * n8n-inspired node accent color. Used for MiniMap, port fallback,
 * and subtle tinting — NOT for heavy decoration.
 */
export function nodeColor(type: string): string {
  switch (type) {
    case "agent":
      return "#9333EA"; // purple — AI agents
    case "file_input":
      return "#0284C7"; // sky — single file input
    case "folder_input":
      return "#0F766E"; // teal — folder input
    case "input_bucket":
      return "#0D9488"; // teal — object-store read
    case "output_bucket":
      return "#0369A1"; // sky-dark — object-store write
    case "output_folder":
      return "#15803D"; // green — local artifact write
    case "wait_until":
    case "wait_for_event":
      return "#334155"; // slate — temporal/event waits
    case "parallel":
      return "#2563EB"; // blue — fan-out
    case "join":
      return "#0EA5E9"; // sky — fan-in
    case "for_each":
      return "#0891B2"; // cyan — iteration
    case "loop":
      return "#0F766E"; // teal — bounded control loop
    case "error_boundary":
      return "#B91C1C"; // red — failure isolation
    case "subworkflow":
      return "#7C3AED"; // violet — composed workflow
    case "tool":
      return "#2563EB"; // blue — direct registered tool call
    case "mcp_resource":
      return "#1D4ED8"; // blue — MCP resource/tool call
    case "webhook":
      return "#0891B2"; // cyan — outbound HTTP request
    case "api_request":
      return "#0E7490"; // deep cyan — HTTP API request (URL / cURL)
    case "knowledge_query":
      return "#0F766E"; // teal — KB / GraphRAG retrieval
    case "knowledge_build":
      return "#0D9488"; // teal — KB build / refresh
    case "template":
      return "#7C2D12"; // amber-brown — prompt/payload shaping
    case "external_app":
      return "#EA580C"; // orange — migration bridge to existing apps
    case "python_code":
      return "#4F46E5"; // indigo — custom Python
    case "router":
      return "#CA8A04"; // amber — logic / routing
    case "guardrail":
      return "#DC2626"; // red — safety
    case "human_approval":
      return "#7C3AED"; // violet — human-in-loop
    case "start":
    case "output":
      return "#059669"; // emerald — I/O endpoints
    default:
      return "#6B7280"; // gray — unknown
  }
}

export function nodeGuide(
  node: ManifestNode,
  componentSpec: WorkflowComponent | null = null,
  manifest: WorkflowManifest | null = null,
): NodeGuide {
  const definition = NODE_GUIDANCE[node.type];
  const fallbackChecks = (definition.checks ?? [])
    .filter((check) => (check.when ? check.when(node, manifest) : true))
    .map((check) => ({
      label: check.label,
      help: check.help,
      satisfied: check.test(node, manifest),
    }));
  const serverChecks = (componentSpec?.setup_checks ?? []).map((check) => ({
    label: check.label,
    help: check.help,
    satisfied: evaluateComponentSetupCheck(node, check, manifest),
  }));
  const checks = [...serverChecks];
  const seenLabels = new Set(serverChecks.map((check) => check.label));
  for (const check of fallbackChecks) {
    if (seenLabels.has(check.label)) continue;
    seenLabels.add(check.label);
    checks.push(check);
  }
  return {
    summary: componentSpec?.description || definition.summary,
    tips: componentSpec?.docs?.length
      ? componentSpec.docs
      : (definition.tips ?? []),
    checks,
    missingLabels: checks
      .filter((check) => !check.satisfied)
      .map((check) => check.label),
  };
}

export function nodeValidationIssues(
  report: ValidationReport | null | undefined,
  nodeId: string,
): ValidationIssue[] {
  if (!report) return [];
  const prefix = `nodes.${nodeId}`;
  return [...report.errors, ...report.warnings].filter(
    (issue) => issue.path === prefix || issue.path.startsWith(`${prefix}.`),
  );
}

export function nodeFieldValidationIssues(
  report: ValidationReport | null | undefined,
  nodeId: string,
  fieldKey: string,
): ValidationIssue[] {
  if (!report) return [];
  const prefix = nodeFieldPathPrefix(nodeId, fieldKey);
  return [...report.errors, ...report.warnings].filter((issue) =>
    matchesNodeFieldPath(issue.path, prefix),
  );
}

export function nodeFieldSetupChecks(
  node: ManifestNode,
  componentSpec: WorkflowComponent | null = null,
  fieldKey: string,
  manifest: WorkflowManifest | null = null,
): NodeGuideCheck[] {
  return (componentSpec?.setup_checks ?? [])
    .filter((check) => componentSetupCheckTouchesField(check, fieldKey))
    .map((check) => ({
      label: check.label,
      help: check.help,
      satisfied: evaluateComponentSetupCheck(node, check, manifest),
    }));
}

export function nodeValidationSummary(
  node: ManifestNode,
  report: ValidationReport | null | undefined,
  componentSpec: WorkflowComponent | null = null,
  manifest: WorkflowManifest | null = null,
): NodeValidationSummary {
  const issues = nodeValidationIssues(report, node.id);
  const errors = issues.filter((issue) => issue.severity === "error").length;
  const warnings = issues.length - errors;
  const guide = nodeGuide(node, componentSpec, manifest);
  if (errors > 0) {
    return {
      severity: "error",
      errors,
      warnings,
      missingLabels: guide.missingLabels,
      title: issues.map((issue) => issue.message).join(" • "),
    };
  }
  if (warnings > 0) {
    return {
      severity: "warning",
      errors,
      warnings,
      missingLabels: guide.missingLabels,
      title: issues.map((issue) => issue.message).join(" • "),
    };
  }
  if (guide.missingLabels.length > 0) {
    return {
      severity: "setup",
      errors,
      warnings,
      missingLabels: guide.missingLabels,
      title: `Needs setup: ${guide.missingLabels.join(", ")}`,
    };
  }
  return {
    severity: "ok",
    errors,
    warnings,
    missingLabels: [],
    title: "Configuration checklist complete.",
  };
}

/**
 * n8n-inspired data-type port color.
 *
 * 14-hue system encoding connection compatibility — port dots and
 * connection lines use these colors so users can visually match
 * compatible ports.
 */
export function portColor(dataType: string): string {
  switch (dataType) {
    case "string":
      return "#4F46E5"; // indigo — text / string data
    case "structured":
      return "#DC2626"; // red — JSON / structured data
    case "messages":
      return "#C026D3"; // fuchsia — message / chat data
    case "boolean":
      return "#CA8A04"; // amber — boolean values
    case "void":
      return "#6B7280"; // gray — no data
    default:
      return "#6B7280"; // gray — unknown
  }
}

function secondaryGraphEdges(manifest: WorkflowManifest): Edge[] {
  const edges: Edge[] = [];

  for (const node of Object.values(manifest.nodes)) {
    if (node.type === "agent" && Array.isArray(node.handoffs)) {
      for (const [index, handoff] of node.handoffs.entries()) {
        edges.push({
          id: `handoff_${node.id}_${index}_${handoff.target}`,
          source: node.id,
          target: handoff.target,
          type: "smoothstep",
          label: "handoff",
          animated: true,
          style: { strokeDasharray: "5 5" },
        });
      }
    }

    if (node.type === "for_each" && typeof node.target_node_id === "string") {
      edges.push({
        id: `for_each_target_${node.id}_${node.target_node_id}`,
        source: node.id,
        target: node.target_node_id,
        type: "smoothstep",
        label: "loop target",
        animated: false,
        style: {
          stroke: "#0F766E",
          strokeWidth: 1.5,
          strokeDasharray: "4 4",
        },
        labelStyle: {
          fontSize: 10,
          fill: "#0F766E",
          fontWeight: 600,
          fontFamily: "Inter, system-ui, sans-serif",
        },
        labelBgStyle: { fill: "#ECFDF5", fillOpacity: 0.95 },
        labelBgPadding: [6, 3] as [number, number],
        labelBgBorderRadius: 6,
      });
    }

    if (node.type === "loop" && typeof node.target_node_id === "string") {
      edges.push({
        id: `loop_target_${node.id}_${node.target_node_id}`,
        source: node.id,
        target: node.target_node_id,
        type: "smoothstep",
        label: "loop target",
        animated: false,
        style: {
          stroke: "#0F766E",
          strokeWidth: 1.5,
          strokeDasharray: "4 4",
        },
        labelStyle: {
          fontSize: 10,
          fill: "#0F766E",
          fontWeight: 600,
          fontFamily: "Inter, system-ui, sans-serif",
        },
        labelBgStyle: { fill: "#ECFDF5", fillOpacity: 0.95 },
        labelBgPadding: [6, 3] as [number, number],
        labelBgBorderRadius: 6,
      });
    }

    if (node.type === "error_boundary") {
      if (typeof node.target_node_id === "string") {
        edges.push({
          id: `error_boundary_target_${node.id}_${node.target_node_id}`,
          source: node.id,
          target: node.target_node_id,
          type: "smoothstep",
          label: "protected",
          animated: false,
          style: {
            stroke: "#B45309",
            strokeWidth: 1.5,
            strokeDasharray: "4 4",
          },
          labelStyle: {
            fontSize: 10,
            fill: "#B45309",
            fontWeight: 600,
            fontFamily: "Inter, system-ui, sans-serif",
          },
          labelBgStyle: { fill: "#FFFBEB", fillOpacity: 0.95 },
          labelBgPadding: [6, 3] as [number, number],
          labelBgBorderRadius: 6,
        });
      }
      if (typeof node.compensate_with === "string") {
        edges.push({
          id: `error_boundary_compensate_${node.id}_${node.compensate_with}`,
          source: node.id,
          target: node.compensate_with,
          type: "smoothstep",
          label: "compensates",
          animated: false,
          style: {
            stroke: "#BE123C",
            strokeWidth: 1.5,
            strokeDasharray: "4 4",
          },
          labelStyle: {
            fontSize: 10,
            fill: "#BE123C",
            fontWeight: 600,
            fontFamily: "Inter, system-ui, sans-serif",
          },
          labelBgStyle: { fill: "#FFF1F2", fillOpacity: 0.95 },
          labelBgPadding: [6, 3] as [number, number],
          labelBgBorderRadius: 6,
        });
      }
    }
  }

  return edges;
}

/** Compute BFS depth (column) for each node id starting from start nodes. */
function computeDepths(manifest: WorkflowManifest): Record<string, number> {
  const adjacency: Record<string, string[]> = {};
  for (const id of Object.keys(manifest.nodes)) adjacency[id] = [];
  for (const edge of manifest.edges) {
    adjacency[edge.from]?.push(edge.to);
  }
  for (const edge of secondaryGraphEdges(manifest)) {
    adjacency[edge.source]?.push(edge.target);
  }
  const starts = Object.values(manifest.nodes)
    .filter((n) => n.type === "start")
    .map((n) => n.id);
  const depth: Record<string, number> = {};
  const queue: Array<[string, number]> = starts.map((id) => [id, 0]);
  while (queue.length > 0) {
    const item = queue.shift();
    if (!item) break;
    const [id, d] = item;
    const existing = depth[id];
    if (existing !== undefined && existing >= d) continue;
    depth[id] = Math.max(existing ?? 0, d);
    for (const next of adjacency[id] ?? []) {
      queue.push([next, d + 1]);
    }
  }
  // Any unreached node gets depth 0 so it's still placed.
  for (const id of Object.keys(manifest.nodes)) {
    if (depth[id] === undefined) depth[id] = 0;
  }
  return depth;
}

/** Convert a manifest to React Flow nodes + edges with a layered layout. */
export function manifestToFlow(
  manifest: WorkflowManifest,
  nodePositions: Record<string, FlowNodePosition> = {},
  componentSpecs: ReadonlyMap<
    WorkflowNodeType,
    WorkflowComponent
  > | null = null,
): {
  nodes: Node<FlowNodeData>[];
  edges: Edge[];
} {
  const depths = computeDepths(manifest);
  const rowByDepth: Record<number, number> = {};
  const nodes: Node<FlowNodeData>[] = Object.values(manifest.nodes).map(
    (node) => {
      const depth = depths[node.id] ?? 0;
      const row = rowByDepth[depth] ?? 0;
      rowByDepth[depth] = row + 1;
      const stored = nodePositions[node.id];
      return {
        id: node.id,
        type: "caliber",
        position: stored ?? { x: depth * COLUMN_WIDTH, y: row * ROW_HEIGHT },
        data: {
          node,
          label: nodeLabel(node),
          componentSpec: componentSpecs?.get(node.type) ?? null,
          manifest,
        },
      };
    },
  );

  const edges: Edge[] = manifest.edges.map((edge) => ({
    id: edge.id,
    source: edge.from,
    target: edge.to,
    type: "smoothstep",
    label: Object.entries(edge.map)
      .map(([from, to]) => `${from}→${to}`)
      .join(", "),
    labelStyle: {
      fontSize: 10,
      fill: "#71717A",
      fontWeight: 500,
      fontFamily: "Inter, system-ui, sans-serif",
    },
    labelBgStyle: { fill: "#FAFAFA", fillOpacity: 0.95 },
    labelBgPadding: [6, 3] as [number, number],
    labelBgBorderRadius: 6,
    style: { stroke: "#A1A1AA", strokeWidth: 1.5 },
    pathOptions: { borderRadius: 16 },
    animated: false,
  }));

  edges.push(...secondaryGraphEdges(manifest));

  return { nodes, edges };
}

/** Starter templates for the New-Workflow gallery (§16.7.5). */
function agentTemplateNode(
  nodeId: string,
  name: string,
  instructions: string,
): ManifestNode {
  return {
    id: nodeId,
    type: "agent",
    name,
    model: "inherit",
    instructions: { type: "inline", text: instructions },
    tools: [],
    inputs: { input: { type: "string" }, history: { type: "structured" } },
    outputs: {
      final_output: { type: "string" },
      history: { type: "structured" },
    },
  };
}

function knowledgeQueryTemplateManifest(
  workflowId: string,
  name: string,
  retrievalModes: KnowledgeRetrievalMode[],
): WorkflowManifest {
  const runtime: WorkflowRuntimeConfig = {
    sdk: "openai-agents-python",
    sdk_version_policy: "runtime-pinned",
    compiler_version: "caliber-workflow-compiler-v1",
    default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
    session: { type: "none" },
  };
  return {
    schema_version: 1,
    workflow_id: workflowId,
    name,
    runtime,
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { user_message: { type: "string" } },
      },
      knowledge: {
        id: "knowledge",
        type: "knowledge_query",
        knowledge_base_id: "",
        version_ids: [],
        retrieval_modes: [...retrievalModes],
        top_k: 6,
        chat_model: null,
        graph_overrides: null,
        inputs: {
          question: { type: "string" },
          history: { type: "structured" },
          retrieval_modes: { type: "structured" },
          version_ids: { type: "structured" },
          graph_overrides: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          answer: { type: "string" },
          result: { type: "structured" },
          citations: { type: "structured" },
          chunks: { type: "structured" },
          graph_context: { type: "structured" },
        },
      },
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      {
        id: "e_start_knowledge",
        from: "start",
        to: "knowledge",
        map: { user_message: "question" },
      },
      {
        id: "e_knowledge_final",
        from: "knowledge",
        to: "final",
        map: { answer: "response" },
      },
    ],
    tools: {},
  };
}

function knowledgeAgeBuildTemplateManifest(
  workflowId: string,
  name: string,
): WorkflowManifest {
  const runtime: WorkflowRuntimeConfig = {
    sdk: "openai-agents-python",
    sdk_version_policy: "runtime-pinned",
    compiler_version: "caliber-workflow-compiler-v1",
    default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
    session: { type: "none" },
  };
  return {
    schema_version: 1,
    workflow_id: workflowId,
    name,
    runtime,
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { user_message: { type: "string" } },
      },
      build_graph: {
        id: "build_graph",
        type: "knowledge_build",
        knowledge_base_id: "",
        chunking_strategy: "recursive",
        embedding_model: "sentence-transformers/all-MiniLM-L6-v2",
        chunking_config: { chunk_size: 1200, chunk_overlap: 180 },
        graph_config: {
          extractor_backend: "heuristic",
          spacy_model: null,
          max_entities_per_chunk: 12,
          entity_types: [],
          minimum_entity_mentions: 1,
          minimum_relationship_weight: 1,
          default_retrieval_mode: "age_graph",
          retrieval_strength: "balanced",
          output_target: "object_store_and_age",
          age_seed_mode: "entity_then_text",
          age_traversal_hops: 1,
          age_candidate_pool_size: 24,
          age_dense_rerank_weight: 0.35,
          strict_age_retrieval_default: false,
        },
        activate_when_complete: true,
        wait_for_completion: true,
        wait_timeout_seconds: 900,
        inputs: {
          input: { type: "string" },
          sources: { type: "structured" },
          chunking_strategy: { type: "string" },
          embedding_model: { type: "string" },
          chunking_config: { type: "structured" },
          graph_config: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          knowledge_base: { type: "structured" },
          version: { type: "structured" },
          run: { type: "structured" },
          status: { type: "string" },
          version_id: { type: "string" },
          run_id: { type: "string" },
        },
      },
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      {
        id: "e_start_build",
        from: "start",
        to: "build_graph",
        map: { user_message: "input" },
      },
      {
        id: "e_build_final",
        from: "build_graph",
        to: "final",
        map: { text: "response" },
      },
    ],
    tools: {},
  };
}

function eventResumeTemplateManifest(
  workflowId: string,
  name: string,
): WorkflowManifest {
  const runtime: WorkflowRuntimeConfig = {
    sdk: "openai-agents-python",
    sdk_version_policy: "runtime-pinned",
    compiler_version: "caliber-workflow-compiler-v1",
    default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
    session: { type: "none" },
  };
  return {
    schema_version: 1,
    workflow_id: workflowId,
    name,
    runtime,
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { user_message: { type: "string" } },
      },
      wait_gate: {
        id: "wait_gate",
        type: "wait_for_event",
        event_name: "documents.ready",
        correlation_key: "document_id",
        timeout_seconds: 3600,
        inputs: { input: { type: "string" } },
        outputs: {
          output: { type: "string" },
          event_payload: { type: "structured" },
          event_name: { type: "string" },
        },
      },
      agent: agentTemplateNode(
        "agent",
        "release-agent",
        "Once the external readiness event arrives, summarize what changed, what is ready now, and what the operator should do next.",
      ),
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      {
        id: "e_start_wait",
        from: "start",
        to: "wait_gate",
        map: { user_message: "input" },
      },
      {
        id: "e_wait_agent",
        from: "wait_gate",
        to: "agent",
        map: { output: "input" },
      },
      {
        id: "e_agent_final",
        from: "agent",
        to: "final",
        map: { final_output: "response" },
      },
    ],
    tools: {},
  };
}

export function templateManifest(
  kind: WorkflowTemplateKind,
  workflowId: string,
  name: string,
): WorkflowManifest {
  const runtime: WorkflowRuntimeConfig = {
    sdk: "openai-agents-python",
    sdk_version_policy: "runtime-pinned",
    compiler_version: "caliber-workflow-compiler-v1",
    default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
    session: { type: "none" },
  };
  const base: WorkflowManifest = {
    schema_version: 1,
    workflow_id: workflowId,
    name,
    runtime,
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { user_message: { type: "string" } },
      },
      agent: agentTemplateNode(
        "agent",
        "main-agent",
        "You are a helpful assistant.",
      ),
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      {
        id: "e_start_agent",
        from: "start",
        to: "agent",
        map: { user_message: "input" },
      },
      {
        id: "e_agent_final",
        from: "agent",
        to: "final",
        map: { final_output: "response" },
      },
    ],
    tools: {},
  };

  if (kind === "blank") {
    return {
      schema_version: 1,
      workflow_id: workflowId,
      name,
      runtime,
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [],
      tools: {},
    };
  }

  if (kind === "knowledge_rag") {
    return knowledgeQueryTemplateManifest(workflowId, name, []);
  }

  if (kind === "graph_hybrid_rag") {
    return knowledgeQueryTemplateManifest(workflowId, name, ["graph_hybrid"]);
  }

  if (kind === "knowledge_age") {
    return knowledgeQueryTemplateManifest(workflowId, name, ["age_graph"]);
  }

  if (kind === "knowledge_age_build") {
    return knowledgeAgeBuildTemplateManifest(workflowId, name);
  }

  if (kind === "event_resume") {
    return eventResumeTemplateManifest(workflowId, name);
  }

  if (kind === "multi_agent_handoff") {
    base.nodes.agent = {
      ...agentTemplateNode(
        "agent",
        "triage-agent",
        "Handle general requests. When the request is about billing, invoices, or refunds, delegate it to the billing specialist.",
      ),
      handoffs: [
        {
          target: "billing",
          description: "Handle billing, invoices, and refunds.",
          condition:
            "'billing' in input or 'invoice' in input or 'refund' in input",
          input_filter:
            "Billing handoff\nCustomer request: {{input}}\nCoordinator draft: {{final_output}}",
        },
      ],
    };
    base.nodes.billing = agentTemplateNode(
      "billing",
      "billing-agent",
      "Resolve billing issues, refunds, invoices, and payment questions.",
    );
    return base;
  }

  if (kind === "parallel_fanout") {
    return {
      schema_version: 1,
      workflow_id: workflowId,
      name,
      runtime,
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        parallel: {
          id: "parallel",
          type: "parallel",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
        research: agentTemplateNode(
          "research",
          "research-agent",
          "Summarize the request from a research and evidence perspective.",
        ),
        writer: agentTemplateNode(
          "writer",
          "writer-agent",
          "Draft a concise answer or action plan for the same request.",
        ),
        join_all: {
          id: "join_all",
          type: "join",
          mode: "all",
          inputs: {
            research: { type: "string" },
            draft: { type: "string" },
          },
          outputs: {
            output: { type: "string" },
            merged: { type: "structured" },
          },
        },
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e_start_parallel",
          from: "start",
          to: "parallel",
          map: { user_message: "input" },
        },
        {
          id: "e_parallel_research",
          from: "parallel",
          to: "research",
          map: { output: "input" },
        },
        {
          id: "e_parallel_writer",
          from: "parallel",
          to: "writer",
          map: { output: "input" },
        },
        {
          id: "e_research_join",
          from: "research",
          to: "join_all",
          map: { final_output: "research" },
        },
        {
          id: "e_writer_join",
          from: "writer",
          to: "join_all",
          map: { final_output: "draft" },
        },
        {
          id: "e_join_final",
          from: "join_all",
          to: "final",
          map: { output: "response" },
        },
      ],
      tools: {},
    };
  }

  if (kind === "guarded_pipeline") {
    base.nodes.guardrail = {
      id: "guardrail",
      type: "guardrail",
      mode: "post_agent",
      inputs: { response: { type: "string" } },
      outputs: { passthrough: { type: "string" } },
      on_failure: "block",
      checks: [{ non_empty_output: {} }],
    };
    base.edges = [
      {
        id: "e_start_agent",
        from: "start",
        to: "agent",
        map: { user_message: "input" },
      },
      {
        id: "e_agent_guard",
        from: "agent",
        to: "guardrail",
        map: { final_output: "response" },
      },
      {
        id: "e_guard_final",
        from: "guardrail",
        to: "final",
        map: { passthrough: "response" },
      },
    ];
  }

  if (kind === "hitl_review") {
    // Governance showcase: PII-redact guardrail + human-in-the-loop approval
    // before the output (agent → redact → review → output).
    base.nodes.pii_guard = {
      id: "pii_guard",
      type: "guardrail",
      mode: "post_agent",
      inputs: { response: { type: "string" } },
      outputs: { clean: { type: "string" } },
      on_failure: "redact",
      checks: [
        {
          pii_detection: { entities: ["email", "ssn", "phone", "credit_card"] },
        },
      ],
    };
    base.nodes.review = {
      id: "review",
      type: "human_approval",
      inputs: { response: { type: "string" } },
      outputs: { approved: { type: "string" } },
    };
    base.edges = [
      {
        id: "e_start_agent",
        from: "start",
        to: "agent",
        map: { user_message: "input" },
      },
      {
        id: "e_agent_guard",
        from: "agent",
        to: "pii_guard",
        map: { final_output: "response" },
      },
      {
        id: "e_guard_review",
        from: "pii_guard",
        to: "review",
        map: { clean: "response" },
      },
      {
        id: "e_review_final",
        from: "review",
        to: "final",
        map: { approved: "response" },
      },
    ];
  }

  if (kind === "for_each_loop") {
    return {
      schema_version: 1,
      workflow_id: workflowId,
      name,
      runtime,
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        for_each: {
          id: "for_each",
          type: "for_each",
          target_node_id: "worker",
          item_input_port: "items",
          max_items: 100,
          inputs: { items: { type: "structured" } },
          outputs: {
            results: { type: "structured" },
            text: { type: "string" },
            metadata: { type: "structured" },
          },
        },
        worker: agentTemplateNode(
          "worker",
          "item-worker",
          "Process one list item at a time. Return a short answer for the current item only.",
        ),
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e_start_loop",
          from: "start",
          to: "for_each",
          map: { user_message: "items" },
        },
        {
          id: "e_loop_final",
          from: "for_each",
          to: "final",
          map: { text: "response" },
        },
      ],
      tools: {},
    };
  }

  if (kind === "refinement_loop") {
    return {
      schema_version: 1,
      workflow_id: workflowId,
      name,
      runtime,
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        loop: {
          id: "loop",
          type: "loop",
          target_node_id: "editor",
          max_iterations: 3,
          stop_condition: "iteration >= 2",
          inputs: {
            input: { type: "string" },
            state: { type: "structured" },
          },
          outputs: {
            output: { type: "string" },
            result: { type: "structured" },
            iterations: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        editor: agentTemplateNode(
          "editor",
          "editor-agent",
          "Refine the current draft once. Return only the improved draft, without commentary.",
        ),
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e_start_loop",
          from: "start",
          to: "loop",
          map: { user_message: "input" },
        },
        {
          id: "e_loop_final",
          from: "loop",
          to: "final",
          map: { output: "response" },
        },
      ],
      tools: {},
    };
  }

  return base;
}

/** Declared output port names for a node. */
export function nodeOutputs(node: ManifestNode): string[] {
  return Object.keys(node.outputs ?? {});
}

/** Declared input port names for a node. */
export function nodeInputs(node: ManifestNode): string[] {
  return Object.keys(node.inputs ?? {});
}

/** Client-side mirror of the workflow port assignability rules. */
export function portSpecAssignable(
  target: Pick<PortSpec, "type"> | null | undefined,
  source: Pick<PortSpec, "type"> | null | undefined,
): boolean {
  const targetType = target?.type;
  const sourceType = source?.type;
  if (!targetType || !sourceType) return true;
  if (targetType === sourceType) return true;
  return targetType === "messages" && sourceType === "string";
}

/** Type-compatible source outputs for one target input. */
export function compatibleSourceOutputs(
  source: ManifestNode,
  target: ManifestNode,
  inputName: string,
): string[] {
  const targetSpec = target.inputs?.[inputName];
  return nodeOutputs(source).filter((outputName) =>
    portSpecAssignable(targetSpec, source.outputs?.[outputName]),
  );
}

/** Whether a direct source -> target edge has at least one valid port contract. */
export function canConnectNodes(
  source: ManifestNode,
  target: ManifestNode,
): boolean {
  const outputs = nodeOutputs(source);
  const inputs = nodeInputs(target);
  if (outputs.length === 0 || inputs.length === 0) return false;
  return inputs.some(
    (inputName) =>
      compatibleSourceOutputs(source, target, inputName).length > 0,
  );
}

/**
 * Type-aware auto-map between two nodes.
 *
 * Matches identical compatible names first, then fills remaining target inputs
 * with the next free compatible source output.
 */
export function autoMapCompatiblePorts(
  source: ManifestNode,
  target: ManifestNode,
): Record<string, string> {
  const outputs = nodeOutputs(source);
  const inputs = nodeInputs(target);
  const map: Record<string, string> = {};
  const usedInputs = new Set<string>();
  const usedOutputs = new Set<string>();

  for (const out of outputs) {
    if (
      inputs.includes(out) &&
      portSpecAssignable(target.inputs?.[out], source.outputs?.[out])
    ) {
      map[out] = out;
      usedInputs.add(out);
      usedOutputs.add(out);
    }
  }

  const freeInputs = inputs.filter((inputName) => !usedInputs.has(inputName));
  for (const inputName of freeInputs) {
    const match = outputs.find(
      (outputName) =>
        !usedOutputs.has(outputName) &&
        portSpecAssignable(
          target.inputs?.[inputName],
          source.outputs?.[outputName],
        ),
    );
    if (!match) continue;
    map[match] = inputName;
    usedInputs.add(inputName);
    usedOutputs.add(match);
  }

  return map;
}

/**
 * Auto-map a source node's outputs onto a target node's inputs (§16.7.4).
 *
 * Matches by identical port name first, then fills remaining target inputs
 * positionally from the leftover source outputs. Returns a
 * ``{source_output: target_input}`` map (the manifest edge ``map`` shape).
 */
export function autoMapPorts(
  outputs: string[],
  inputs: string[],
): Record<string, string> {
  const map: Record<string, string> = {};
  const usedInputs = new Set<string>();
  const usedOutputs = new Set<string>();
  for (const out of outputs) {
    if (inputs.includes(out)) {
      map[out] = out;
      usedInputs.add(out);
      usedOutputs.add(out);
    }
  }
  const freeOutputs = outputs.filter((o) => !usedOutputs.has(o));
  const freeInputs = inputs.filter((i) => !usedInputs.has(i));
  for (let i = 0; i < Math.min(freeOutputs.length, freeInputs.length); i += 1) {
    map[freeOutputs[i]!] = freeInputs[i]!;
  }
  return map;
}

/** Derive a default data map for an edge between two nodes. */
export function deriveEdgeMap(
  source: ManifestNode,
  target: ManifestNode,
): Record<string, string> {
  const outputs = nodeOutputs(source);
  const inputs = nodeInputs(target);
  const map = autoMapCompatiblePorts(source, target);
  if (Object.keys(map).length > 0) return map;
  // When one side has no declared ports, keep a placeholder pair so the user
  // can still open the edge editor and decide what the contract should be.
  if (outputs.length === 0 || inputs.length === 0) {
    const out = outputs[0] ?? "output";
    const inp = inputs[0] ?? "input";
    return { [out]: inp };
  }
  return {};
}

/** Stable, collision-resistant edge id for a source→target connection. */
export function makeEdgeId(
  source: string,
  target: string,
  existing: Set<string>,
): string {
  const base = `e_${source}_${target}`;
  if (!existing.has(base)) return base;
  let n = 2;
  while (existing.has(`${base}_${n}`)) n += 1;
  return `${base}_${n}`;
}

function majorVersion(version: string): number {
  const major = Number.parseInt(version.split(".", 1)[0] ?? "", 10);
  return Number.isFinite(major) && major > 0 ? major : 1;
}

/** Registry reference used by workflow manifest tool bindings. */
export function registryRefForTool(
  tool: Pick<ToolDefinition, "name" | "version">,
): string {
  return `tool.${tool.name}.v${majorVersion(tool.version)}`;
}

/** Default semver range for agent tool bindings created from the registry. */
export function versionConstraintForTool(
  tool: Pick<ToolDefinition, "version">,
): string {
  const major = majorVersion(tool.version);
  return `>=${major}.0,<${major + 1}.0`;
}

/** Manifest binding payload that lets Agents SDK workflows resolve a tool. */
export function toolBindingForDefinition(
  tool: ToolDefinition,
): WorkflowToolBinding {
  return {
    registry_ref: registryRefForTool(tool),
    version_constraint: versionConstraintForTool(tool),
    requires_approval: tool.requires_approval,
    timeout_seconds: tool.side_effect_level === "read" ? 30 : 60,
    max_retries: 0,
  };
}

/**
 * Ensure every agent-selected or tool-node-selected tool has a top-level manifest binding.
 *
 * The editor stores selected tool names on agent and tool nodes, while the compiler
 * resolves concrete registry entries from ``manifest.tools``. This sync step is
 * what turns a wizard-registered tool into a workflow-usable binding.
 */
export function ensureAgentToolBindings(
  manifest: WorkflowManifest,
  registeredTools: ToolDefinition[],
  mcpServers: McpServer[] = [],
): WorkflowManifest {
  const toolByName = new Map(registeredTools.map((tool) => [tool.name, tool]));
  const mcpByName = new Map(
    mcpServers.map((server) => [server.name.toLowerCase(), server]),
  );
  const nextTools: Record<string, WorkflowToolBinding> = {
    ...(manifest.tools ?? {}),
  };
  for (const node of Object.values(manifest.nodes)) {
    const referencedTools =
      node.type === "agent"
        ? (node.tools ?? [])
        : node.type === "tool" &&
            typeof node.tool_name === "string" &&
            node.tool_name.trim()
          ? [node.tool_name.trim()]
          : [];
    for (const toolName of referencedTools) {
      if (nextTools[toolName]) continue;
      const definition = toolByName.get(toolName);
      if (definition) {
        nextTools[toolName] = toolBindingForDefinition(definition);
        continue;
      }
      if (!toolName.startsWith("mcp:")) continue;
      const rawRef = toolName.slice(4);
      const slash = rawRef.indexOf("/");
      if (slash <= 0) continue;
      const serverName = rawRef.slice(0, slash).trim().toLowerCase();
      const mcpToolName = rawRef.slice(slash + 1).trim();
      if (!serverName || !mcpToolName) continue;
      const server = mcpByName.get(serverName);
      if (!server) continue;
      nextTools[toolName] = {
        type: "mcp_tool",
        server_id: server.server_id,
        tool_name: mcpToolName,
        side_effect_level: "read",
        requires_approval: false,
        max_retries: 0,
      };
    }
  }
  return { ...manifest, tools: nextTools };
}

const NODE_PALETTE_BY_TYPE: Record<WorkflowNodeType, NodePaletteItem> = {
  start: {
    type: "start",
    label: "Start",
    group: "Inputs & Outputs",
    description: "Entry point of the flow",
    docs: [],
  },
  file_input: {
    type: "file_input",
    label: "File Input",
    group: "Inputs & Outputs",
    description: "Read one file into the flow",
    docs: [],
  },
  folder_input: {
    type: "folder_input",
    label: "Input Folder",
    group: "Inputs & Outputs",
    description: "Read matching files from a local folder",
    docs: [],
  },
  input_bucket: {
    type: "input_bucket",
    label: "Input Bucket",
    group: "Inputs & Outputs",
    description: "Read bounded text objects from an object-storage bucket",
    docs: [],
  },
  output_bucket: {
    type: "output_bucket",
    label: "Output Bucket",
    group: "Inputs & Outputs",
    description: "Write run artifacts to a storage bucket",
    docs: [],
  },
  output_folder: {
    type: "output_folder",
    label: "Output Folder",
    group: "Inputs & Outputs",
    description: "Write run artifacts to a local folder",
    docs: [],
  },
  wait_until: {
    type: "wait_until",
    label: "Wait Until",
    group: "Orchestration",
    description: "Pause until a configured time",
    docs: [],
  },
  wait_for_event: {
    type: "wait_for_event",
    label: "Wait For Event",
    group: "Orchestration",
    description: "Pause until resumed by event",
    docs: [],
  },
  parallel: {
    type: "parallel",
    label: "Parallel",
    group: "Orchestration",
    description: "Fan out execution paths",
    docs: [],
  },
  join: {
    type: "join",
    label: "Join",
    group: "Orchestration",
    description: "Merge incoming branches",
    docs: [],
  },
  for_each: {
    type: "for_each",
    label: "For Each",
    group: "Orchestration",
    description: "Iterate over input items",
    docs: [],
  },
  loop: {
    type: "loop",
    label: "Loop",
    group: "Orchestration",
    description: "Repeat one target until a stop condition matches",
    docs: [],
  },
  error_boundary: {
    type: "error_boundary",
    label: "Error Boundary",
    group: "Safety",
    description: "Catch failures with fallback",
    docs: [],
  },
  subworkflow: {
    type: "subworkflow",
    label: "Subworkflow",
    group: "Orchestration",
    description: "Invoke another workflow",
    docs: [],
  },
  tool: {
    type: "tool",
    label: "Tool",
    group: "Integrations",
    description: "Invoke a registered tool binding directly",
    docs: [],
  },
  mcp_resource: {
    type: "mcp_resource",
    label: "MCP Resource",
    group: "Integrations",
    description: "Invoke an MCP server tool",
    docs: [],
  },
  webhook: {
    type: "webhook",
    label: "Webhook",
    group: "Integrations",
    description: "Send an outbound HTTP request to an external URL",
    docs: [],
  },
  api_request: {
    type: "api_request",
    label: "API Request",
    group: "Integrations",
    description: "Make HTTP requests using a URL or cURL command",
    docs: [],
  },
  knowledge_query: {
    type: "knowledge_query",
    label: "Knowledge Query",
    group: "Integrations",
    description: "Query a KB with dense, GraphRAG, or AGE retrieval",
    docs: [],
  },
  knowledge_build: {
    type: "knowledge_build",
    label: "Knowledge Build",
    group: "Integrations",
    description: "Launch a KB version build or refresh",
    docs: [],
  },
  template: {
    type: "template",
    label: "Template",
    group: "Utilities",
    description: "Render a no-code prompt or JSON payload",
    docs: [],
  },
  external_app: {
    type: "external_app",
    label: "External App",
    group: "Integrations",
    description: "Invoke an existing Python app entrypoint during migration",
    docs: [],
  },
  python_code: {
    type: "python_code",
    label: "Python Code",
    group: "Utilities",
    description: "Run custom sandboxed Python",
    docs: [],
  },
  output: {
    type: "output",
    label: "Output",
    group: "Inputs & Outputs",
    description: "Final response endpoint",
    docs: [],
  },
  agent: {
    type: "agent",
    label: "Agent",
    group: "Agents",
    description: "LLM-powered reasoning step",
    docs: [],
  },
  router: {
    type: "router",
    label: "Router",
    group: "Logic",
    description: "Conditional branch routing",
    docs: [],
  },
  guardrail: {
    type: "guardrail",
    label: "Guardrail",
    group: "Safety",
    description: "Content safety check",
    docs: [],
  },
  human_approval: {
    type: "human_approval",
    label: "Human Approval",
    group: "Safety",
    description: "Manual review gate",
    docs: [],
  },
  note: {
    type: "note",
    label: "Note",
    group: "Utilities",
    description: "Annotation or comment",
    docs: [],
  },
};

export const NODE_PALETTE: NodePaletteItem[] = Object.values(
  NODE_PALETTE_BY_TYPE,
);

export function isWorkflowNodeType(value: string): value is WorkflowNodeType {
  return Object.prototype.hasOwnProperty.call(NODE_PALETTE_BY_TYPE, value);
}

export function buildNodePalette(
  components: WorkflowComponent[] | null | undefined,
): NodePaletteItem[] {
  if (!Array.isArray(components) || components.length === 0) {
    return NODE_PALETTE;
  }
  const overridesByType = new Map(
    components.map(
      (component) =>
        [
          component.type,
          {
            type: component.type,
            label: component.label,
            group: component.category,
            description: component.description,
            docs: component.docs,
            fieldCount: component.fields.length,
            setupRuleCount: component.setup_checks?.length ?? 0,
            defaultInputCount: Object.keys(component.default_inputs ?? {})
              .length,
            defaultOutputCount: Object.keys(component.default_outputs ?? {})
              .length,
            legacy: component.legacy ?? false,
            legacyReplacement: component.legacy_replacement ?? null,
          },
        ] as const,
    ),
  );
  const merged = NODE_PALETTE.map(
    (item) => overridesByType.get(item.type) ?? item,
  );
  const knownTypes = new Set(merged.map((item) => item.type));
  const extras = components
    .filter((component) => !knownTypes.has(component.type))
    .map((component) => ({
      type: component.type,
      label: component.label,
      group: component.category,
      description: component.description,
      docs: component.docs,
      fieldCount: component.fields.length,
      setupRuleCount: component.setup_checks?.length ?? 0,
      defaultInputCount: Object.keys(component.default_inputs ?? {}).length,
      defaultOutputCount: Object.keys(component.default_outputs ?? {}).length,
      legacy: component.legacy ?? false,
      legacyReplacement: component.legacy_replacement ?? null,
    }));
  return [...merged, ...extras];
}
