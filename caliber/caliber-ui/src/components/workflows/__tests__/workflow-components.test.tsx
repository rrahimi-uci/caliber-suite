import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { ReactFlowProvider } from "@xyflow/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll } from "vitest";
import { describe, expect, it, vi } from "vitest";

import type {
  GraphDiff as GraphDiffData,
  ManifestNode,
  ToolDefinition,
  ValidationReport,
  WorkflowComponent,
  WorkflowManifest,
} from "@/api/workflowTypes";
import type { WorkflowRun } from "@/api/workflowTypes";
import type { EvalDataset, PromptInfo, Skill } from "@/api/types";
import { CaliberNode } from "@/components/workflows/CaliberNode";
import { ConnectMapPopover } from "@/components/workflows/ConnectMapPopover";
import { NODE_ICON_COMPONENTS } from "@/components/workflows/NodeIcon";
import { GraphDiff } from "@/components/workflows/GraphDiff";
import { Inspector } from "@/components/workflows/Inspector";
import { NodeDetailPanel } from "@/components/workflows/NodeDetailPanel";
import { NodePalette } from "@/components/workflows/NodePalette";
import { ProblemsPanel } from "@/components/workflows/ProblemsPanel";
import { PublishDrawer } from "@/components/workflows/PublishDrawer";
import {
  RouterConditionBuilder,
  type RouterBranch,
} from "@/components/workflows/RouterConditionBuilder";
import { NODE_PALETTE } from "@/lib/workflowGraph";
import { TraceReplayGraph } from "@/components/workflows/TraceReplayGraph";
import { WorkflowComponentSchemaSummary } from "@/components/workflows/WorkflowComponentSchemaSummary";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const OS = `${API_BASE}/object-store`;
const KB = `${API_BASE}/knowledge-bases`;

function envelope<T>(data: T): { data: T } {
  return { data };
}

function objectStoreHandlers() {
  return [
    http.get(`${OS}/buckets`, () =>
      HttpResponse.json(
        envelope([
          { name: "reports", creation_date: null },
          { name: "logs", creation_date: null },
        ]),
      ),
    ),
    http.get(`${OS}/buckets/:bucket/objects`, ({ params, request }) => {
      const url = new URL(request.url);
      return HttpResponse.json(
        envelope({
          bucket: String(params.bucket),
          prefix: url.searchParams.get("prefix") ?? "",
          prefixes: [],
          objects: [
            {
              key: "service/a.jsonl",
              size: 10,
              last_modified: "2026-06-10T12:00:00Z",
              etag: "a",
            },
            {
              key: "service/b.jsonl",
              size: 11,
              last_modified: "2026-06-10T12:01:00Z",
              etag: "b",
            },
          ],
          next_token: null,
          is_truncated: false,
        }),
      );
    }),
  ];
}

function knowledgeHandlers({
  embeddingUnavailableReason = null,
}: {
  embeddingUnavailableReason?: string | null;
} = {}) {
  return [
    http.get(`${KB}/options`, () =>
      HttpResponse.json(
        envelope({
          chunking_strategies: [
            {
              id: "recursive_character",
              name: "Recursive character",
              description: "Balanced paragraph-aware chunking.",
              defaults: {},
              tags: ["default"],
            },
            {
              id: "semantic",
              name: "Semantic",
              description: "Meaning-aware chunking.",
              defaults: {},
              tags: [],
            },
          ],
          embedding_models: [
            {
              id: "BAAI/bge-base-en-v1.5",
              name: "BAAI / bge-base-en-v1.5",
              description: "General-purpose English embeddings.",
              defaults: {},
              tags: ["default"],
              available: embeddingUnavailableReason == null,
              unavailable_reason: embeddingUnavailableReason,
              requires_override: embeddingUnavailableReason != null,
            },
            {
              id: "intfloat/e5-large-v2",
              name: "intfloat / e5-large-v2",
              description: "High-recall multilingual embeddings.",
              defaults: {},
              tags: [],
              available: embeddingUnavailableReason == null,
              unavailable_reason: embeddingUnavailableReason,
              requires_override: embeddingUnavailableReason != null,
            },
          ],
          retrieval_modes: [
            {
              id: "dense",
              name: "Dense retrieval",
              description: "Vector search",
              defaults: {},
              tags: [],
            },
            {
              id: "graph_hybrid",
              name: "Graph hybrid",
              description: "Graph + vector",
              defaults: {},
              tags: [],
            },
            {
              id: "age_graph",
              name: "Apache AGE",
              description: "AGE traversal",
              defaults: {},
              tags: [],
            },
          ],
          graph_extractors: [],
          graph_output_targets: [],
          graph_retrieval_strengths: [
            {
              id: "conservative",
              name: "Conservative",
              description: "Low recall",
              defaults: {},
              tags: [],
            },
            {
              id: "balanced",
              name: "Balanced",
              description: "Recommended",
              defaults: {},
              tags: [],
            },
            {
              id: "aggressive",
              name: "Aggressive",
              description: "High recall",
              defaults: {},
              tags: [],
            },
          ],
          graph_age_seed_modes: [
            {
              id: "entity_then_text",
              name: "Entity first, then question text",
              description: "Balanced AGE seeding",
              defaults: {},
              tags: ["default"],
            },
            {
              id: "query_entities_only",
              name: "Extracted entities only",
              description: "Entity-only AGE seeding",
              defaults: {},
              tags: [],
            },
            {
              id: "query_text_only",
              name: "Question text only",
              description: "Text-first AGE seeding",
              defaults: {},
              tags: [],
            },
            {
              id: "query_entities_and_text",
              name: "Entities plus question text",
              description: "Broad AGE seeding",
              defaults: {},
              tags: [],
            },
          ],
          graph_entity_types: [],
          graph_query_presets: [
            {
              id: "hybrid_balanced",
              label: "Balanced GraphRAG",
              eyebrow: "Portable",
              description: "Balanced local graph retrieval.",
              badges: ["Local graph", "1-hop", "Balanced"],
              retrieval_mode: "graph_hybrid",
              patch: {
                retrieval_strength: "balanced",
                minimum_relationship_weight: 1,
                age_traversal_hops: 1,
              },
              recommended: false,
              age_required: false,
            },
            {
              id: "age_native",
              label: "AGE-native retrieval",
              eyebrow: "Graph-first",
              description: "Graph-first AGE retrieval.",
              badges: ["AGE primary", "2-hop", "Graph-first"],
              retrieval_mode: "age_graph",
              patch: {
                retrieval_strength: "aggressive",
                minimum_relationship_weight: 1,
                age_seed_mode: "query_entities_and_text",
                age_traversal_hops: 2,
                age_candidate_pool_size: 40,
                age_dense_rerank_weight: 0.2,
              },
              recommended: true,
              age_required: true,
            },
            {
              id: "age_strict",
              label: "Strict AGE only",
              eyebrow: "Locked",
              description: "AGE-only retrieval with no fallback.",
              badges: ["AGE primary", "Strict", "No fallback"],
              retrieval_mode: "age_graph",
              patch: {
                retrieval_strength: "aggressive",
                minimum_relationship_weight: 1,
                age_seed_mode: "query_entities_and_text",
                age_traversal_hops: 2,
                age_candidate_pool_size: 40,
                age_dense_rerank_weight: 0.2,
                strict_age_retrieval: true,
              },
              recommended: false,
              age_required: true,
            },
          ],
          default_graph_config: {
            extractor_backend: "heuristic",
            spacy_model: null,
            max_entities_per_chunk: 12,
            entity_types: [],
            minimum_entity_mentions: 1,
            minimum_relationship_weight: 1,
            default_retrieval_mode: "graph_hybrid",
            retrieval_strength: "balanced",
            output_target: "object_store",
            age_seed_mode: "entity_then_text",
            age_traversal_hops: 1,
            age_candidate_pool_size: 24,
            age_dense_rerank_weight: 0.35,
          },
          age_enabled: true,
          age_graph_name: "knowledge_graph",
          age_unavailable_reason: null,
          reserved_output_prefix: "knowledge/",
        }),
      ),
    ),
    http.get(`${KB}`, () =>
      HttpResponse.json(
        envelope([
          {
            knowledge_base_id: "KB-1",
            project_id: null,
            visibility: "user",
            name: "Contracts KB",
            description: "",
            owner: "@test",
            status: "active",
            source_bucket: "docs",
            source_manifest: [],
            source_fingerprint: "fp-1",
            active_version_id: "KBV-1",
            last_run_id: null,
            last_run_status: null,
            last_run_completed_at: null,
            active_version_summary: {
              knowledge_base_version_id: "KBV-1",
              version_number: 1,
              status: "completed",
              chunking_strategy: "recursive_character",
              embedding_model: "BAAI/bge-base-en-v1.5",
              graph_extractor: "heuristic",
              graph_target: "object_store_and_age",
              default_retrieval_mode: "age_graph",
              retrieval_strength: "balanced",
              graph_profile_id: null,
              graph_profile_label: null,
              age_sync_status: "synced",
            },
            created_at: "2026-06-10T00:00:00Z",
            updated_at: "2026-06-10T00:00:00Z",
          },
          {
            knowledge_base_id: "KB-2",
            project_id: null,
            visibility: "user",
            name: "Policies KB",
            description: "",
            owner: "@test",
            status: "active",
            source_bucket: "docs",
            source_manifest: [],
            source_fingerprint: "fp-2",
            active_version_id: null,
            last_run_id: null,
            last_run_status: null,
            last_run_completed_at: null,
            active_version_summary: null,
            created_at: "2026-06-11T00:00:00Z",
            updated_at: "2026-06-11T00:00:00Z",
          },
        ]),
      ),
    ),
    http.get(`${KB}/:knowledgeBaseId/versions`, () =>
      HttpResponse.json(
        envelope([
          {
            knowledge_base_version_id: "KBV-1",
            knowledge_base_id: "KB-1",
            version_number: 1,
            status: "completed",
            chunking_strategy: "recursive_character",
            chunking_config: {},
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
            },
            embedding_provider: "huggingface",
            embedding_model: "BAAI/bge-base-en-v1.5",
            embedding_dimension: 768,
            source_manifest: [],
            source_fingerprint: "fp-1",
            output_bucket: "docs",
            output_prefix: "knowledge/contracts/v1/",
            chunks_uri: null,
            entities_uri: null,
            relationships_uri: null,
            graph_uri: null,
            manifest_uri: null,
            logs_uri: null,
            stats_uri: null,
            summary: { age_sync_status: "synced" },
            error_summary: null,
            created_by: "@test",
            created_at: "2026-06-10T00:00:00Z",
            completed_at: "2026-06-10T00:02:00Z",
          },
        ]),
      ),
    ),
  ];
}

function renderWithQuery(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  const wrap = (inner: ReactElement) => (
    <QueryClientProvider client={client}>{inner}</QueryClientProvider>
  );
  const rendered = render(wrap(ui));
  return {
    ...rendered,
    rerender: (nextUi: ReactElement) => rendered.rerender(wrap(nextUi)),
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

function manifest(): WorkflowManifest {
  return {
    schema_version: 1,
    workflow_id: "wf",
    name: "WF",
    nodes: {
      agent: {
        id: "agent",
        type: "agent",
        name: "support-agent",
        model: "inherit",
        instructions: { type: "inline", text: "hi" },
        tools: [],
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
      },
    },
    edges: [],
  };
}

function tool(
  name: string,
  level: ToolDefinition["side_effect_level"] = "read",
): ToolDefinition {
  return {
    tool_id: `TL-${name}`,
    name,
    version: "1.0",
    description: "",
    module_path: "m",
    callable_name: name,
    input_schema: null,
    output_schema: null,
    side_effect_level: level,
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: [],
    test_cases: [],
    last_calibration: null,
    owner: "",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: "x",
    updated_at: "x",
  };
}

describe("CaliberNode", () => {
  it("renders label, subtitle, and type", () => {
    const node = manifest().nodes.agent as ManifestNode;
    render(
      <ReactFlowProvider>
        <CaliberNode
          id="agent"
          type="caliber"
          data={{ node, label: "support-agent" }}
          selected={false}
          dragging={false}
          draggable={false}
          selectable
          deletable
          zIndex={0}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
        />
      </ReactFlowProvider>,
    );
    const el = screen.getByTestId("wf-node-agent");
    expect(el).toHaveAttribute("data-node-type", "agent");
    expect(screen.getByText("support-agent")).toBeInTheDocument();
    expect(screen.getByTestId("wf-node-target-agent")).toBeInTheDocument();
    expect(screen.getByTestId("wf-node-source-agent")).toBeInTheDocument();
  });

  it("only renders handles and quick-add controls for valid flow directions", () => {
    const outputNode: ManifestNode = {
      id: "final",
      type: "output",
      inputs: { response: { type: "string" } },
    };
    const noteNode: ManifestNode = {
      id: "note_1",
      type: "note",
      text: "Docs",
    };

    const { rerender } = render(
      <ReactFlowProvider>
        <CaliberNode
          id="final"
          type="caliber"
          data={{ node: outputNode, label: "final", onQuickAdd: vi.fn() }}
          selected={false}
          dragging={false}
          draggable={false}
          selectable
          deletable
          zIndex={0}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
        />
      </ReactFlowProvider>,
    );

    expect(screen.getByTestId("wf-node-target-final")).toBeInTheDocument();
    expect(
      screen.queryByTestId("wf-node-source-final"),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("quick-add-final")).not.toBeInTheDocument();

    rerender(
      <ReactFlowProvider>
        <CaliberNode
          id="note_1"
          type="caliber"
          data={{ node: noteNode, label: "note", onQuickAdd: vi.fn() }}
          selected={false}
          dragging={false}
          draggable={false}
          selectable
          deletable
          zIndex={0}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
        />
      </ReactFlowProvider>,
    );

    expect(
      screen.queryByTestId("wf-node-target-note_1"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("wf-node-source-note_1"),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("quick-add-note_1")).not.toBeInTheDocument();
  });

  it("renders a wired Duplicate button that fires onDuplicate", async () => {
    const onDuplicate = vi.fn();
    const node = manifest().nodes.agent as ManifestNode;
    render(
      <ReactFlowProvider>
        <CaliberNode
          id="agent"
          type="caliber"
          data={{ node, label: "support-agent", onDuplicate }}
          selected
          dragging={false}
          draggable={false}
          selectable
          deletable
          zIndex={0}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
        />
      </ReactFlowProvider>,
    );
    await userEvent.click(screen.getByTestId("duplicate-agent"));
    expect(onDuplicate).toHaveBeenCalledWith("agent");
  });

  it("does not offer Duplicate on the unique start/output nodes", () => {
    const startNode: ManifestNode = {
      id: "start",
      type: "start",
      outputs: { user_message: { type: "string" } },
    };
    render(
      <ReactFlowProvider>
        <CaliberNode
          id="start"
          type="caliber"
          data={{ node: startNode, label: "start", onDuplicate: vi.fn() }}
          selected
          dragging={false}
          draggable={false}
          selectable
          deletable
          zIndex={0}
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
        />
      </ReactFlowProvider>,
    );
    expect(screen.queryByTestId("duplicate-start")).not.toBeInTheDocument();
  });
});

describe("NodePalette", () => {
  it("renders every default workflow node type and fires onAddNode", async () => {
    const onAdd = vi.fn();
    render(<NodePalette onAddNode={onAdd} />);
    for (const item of NODE_PALETTE) {
      expect(screen.getByTestId(`palette-${item.type}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId("palette-human_approval")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("palette-agent"));
    expect(onAdd).toHaveBeenCalledWith("agent");
  });

  it("keeps palette node types aligned with icon coverage", () => {
    expect(new Set(NODE_PALETTE.map((item) => item.type))).toEqual(
      new Set(Object.keys(NODE_ICON_COMPONENTS)),
    );
  });

  it("prefers backend component catalog metadata when provided", () => {
    const onAdd = vi.fn();
    const components: WorkflowComponent[] = [
      {
        type: "agent",
        label: "Agent Runtime",
        category: "Server Components",
        description: "Backend-provided agent description.",
        docs: [],
        default_inputs: {},
        default_outputs: {},
        fields: [],
      },
    ];

    render(<NodePalette onAddNode={onAdd} components={components} />);

    expect(screen.getByText("Server Components")).toBeInTheDocument();
    expect(screen.getByText("Agent Runtime")).toBeInTheDocument();
    expect(
      screen.getByText("Backend-provided agent description."),
    ).toBeInTheDocument();
  });

  it("searches backend docs and surfaces setup metadata badges", async () => {
    const onAdd = vi.fn();
    const components: WorkflowComponent[] = [
      {
        type: "agent",
        label: "Agent Runtime",
        category: "Server Components",
        description: "Backend-provided agent description.",
        docs: ["Escalates billing specialists when a refund needs review."],
        default_inputs: { input: { type: "string" } },
        default_outputs: { final_output: { type: "string" } },
        fields: [
          {
            key: "model",
            label: "Model",
            type: "string",
            required: true,
            default: "gpt-4.1-mini",
            description: "Model override",
            constraints: {},
            examples: [],
          },
          {
            key: "tools",
            label: "Tools",
            type: "list",
            required: false,
            default: [],
            description: "Attached tools",
            constraints: {},
            examples: [],
          },
        ],
        setup_checks: [
          {
            label: "Provide instructions",
            help: "Add inline instructions or reference a prompt.",
            kind: "instructions_present",
          },
        ],
      },
    ];

    render(<NodePalette onAddNode={onAdd} components={components} />);

    await userEvent.type(
      screen.getByPlaceholderText("Search components…"),
      "billing specialists",
    );

    expect(screen.getByText("Agent Runtime")).toBeInTheDocument();
    expect(screen.getByText("2 fields")).toBeInTheDocument();
    expect(screen.getByText("1 setup rule")).toBeInTheDocument();
    expect(screen.getByText("1 in · 1 out")).toBeInTheDocument();
  });

  it("opens a component reference panel before adding a node", async () => {
    const onAdd = vi.fn();
    const components: WorkflowComponent[] = [
      {
        type: "agent",
        label: "Agent Runtime",
        category: "Server Components",
        description: "Backend-provided agent description.",
        docs: ["Escalates billing specialists when a refund needs review."],
        default_inputs: { input: { type: "string" } },
        default_outputs: { final_output: { type: "string" } },
        fields: [
          {
            key: "model",
            label: "Model",
            type: "string",
            required: true,
            default: "gpt-4.1-mini",
            description: "Model override",
            constraints: {},
            examples: [],
          },
        ],
        setup_checks: [
          {
            label: "Provide instructions",
            help: "Add inline instructions or reference a prompt.",
            kind: "instructions_present",
          },
        ],
      },
    ];

    render(<NodePalette onAddNode={onAdd} components={components} />);

    await userEvent.click(screen.getByTestId("palette-inspect-agent"));
    expect(
      screen.getByTestId("palette-component-reference"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("workflow-component-schema-summary"),
    ).toHaveTextContent("Backend-provided agent description.");
    await userEvent.click(screen.getByTestId("palette-reference-add"));
    expect(onAdd).toHaveBeenCalledWith("agent");
    await userEvent.click(screen.getByTestId("palette-reference-close"));
    expect(
      screen.queryByTestId("palette-component-reference"),
    ).not.toBeInTheDocument();
  });
});

describe("WorkflowComponentSchemaSummary", () => {
  it("renders setup rules alongside field constraints", () => {
    render(
      <WorkflowComponentSchemaSummary
        component={{
          type: "file_input",
          label: "File Input",
          category: "Inputs & Outputs",
          description: "Read one file into the workflow.",
          docs: [
            "Useful when a workflow should begin from a single operator-picked file.",
          ],
          default_inputs: { path: { type: "string" } },
          default_outputs: {
            text: { type: "string" },
            metadata: { type: "structured" },
          },
          fields: [
            {
              key: "path",
              label: "Path",
              type: "string",
              required: true,
              default: "",
              description: "Filesystem path to read.",
              constraints: { min_length: 1 },
              examples: ["/tmp/input.txt"],
            },
          ],
          setup_checks: [
            {
              label: "Provide a file path",
              help: "Set the file path directly or map one into the node's path input.",
              kind: "non_empty_string",
              field: "path",
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("Validation & setup rules")).toBeInTheDocument();
    expect(screen.getByText("Starter inputs")).toBeInTheDocument();
    expect(screen.getByText("Starter outputs")).toBeInTheDocument();
    expect(
      screen.getByTestId("workflow-component-setup-check-0"),
    ).toHaveTextContent("Provide a file path");
    expect(
      screen.getByTestId("workflow-component-setup-check-0"),
    ).toHaveTextContent("Non-empty text");
    expect(
      screen.getByTestId("workflow-component-setup-check-0"),
    ).toHaveTextContent("Targets Path");
    expect(
      screen.getByTestId("workflow-component-field-path"),
    ).toHaveTextContent("min length 1");
  });

  it("humanizes graph-aware setup rule kinds in the schema summary", () => {
    render(
      <WorkflowComponentSchemaSummary
        component={{
          type: "parallel",
          label: "Parallel",
          category: "Orchestration",
          description: "Fan out to multiple downstream branches.",
          docs: ["Use a join barrier when the branches should converge again."],
          default_inputs: {},
          default_outputs: {},
          fields: [],
          setup_checks: [
            {
              label: "Add at least two downstream branches",
              help: "Connect this parallel node to at least two downstream branches before using it as a fan-out barrier.",
              kind: "minimum_outgoing_edges",
              minimum: 2,
            },
          ],
        }}
      />,
    );

    expect(
      screen.getByTestId("workflow-component-setup-check-0"),
    ).toHaveTextContent("At least 2 downstream edges");
  });

  it("explains intentional empty port contracts for control-flow and terminal nodes", () => {
    const { rerender } = render(
      <WorkflowComponentSchemaSummary
        component={{
          type: "join",
          label: "Join",
          category: "Orchestration",
          description: "Explicit fan-in barrier node.",
          docs: ["Merge incoming branches back into one control path."],
          default_inputs: {},
          default_outputs: {
            output: { type: "string" },
            merged: { type: "structured" },
          },
          fields: [],
          setup_checks: [],
        }}
      />,
    );

    expect(screen.getByText("Starter inputs")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Join nodes accept one inbound edge per branch instead of named starter inputs. Wire the upstream branches directly into the join barrier.",
      ),
    ).toBeInTheDocument();

    rerender(
      <WorkflowComponentSchemaSummary
        component={{
          type: "router",
          label: "Router",
          category: "Logic",
          description: "Conditional branch selector.",
          docs: [
            "Branches are evaluated top-to-bottom and the first match wins.",
          ],
          default_inputs: { decision: { type: "string" } },
          default_outputs: {},
          fields: [
            {
              key: "branches",
              label: "Branches",
              type: "list<RouterBranch>",
              required: false,
              default: [],
              description:
                "Ordered router branches evaluated from top to bottom.",
              constraints: {},
              examples: [],
            },
          ],
          setup_checks: [],
        }}
      />,
    );

    expect(
      screen.getByText(
        "Router branches are modeled as outgoing edges, so there are no named starter outputs. Add branch destinations to define each control-flow path.",
      ),
    ).toBeInTheDocument();

    rerender(
      <WorkflowComponentSchemaSummary
        component={{
          type: "output",
          label: "Output",
          category: "Inputs & Outputs",
          description: "Final response endpoint.",
          docs: [
            "Map the final downstream field that should be returned as the workflow response.",
          ],
          default_inputs: { response: { type: "string" } },
          default_outputs: {},
          fields: [],
          setup_checks: [],
        }}
      />,
    );

    expect(
      screen.getByText(
        "Output nodes end the workflow response and do not emit downstream ports.",
      ),
    ).toBeInTheDocument();
  });
});

describe("Inspector", () => {
  it("updates workflow-level session memory settings", async () => {
    const m = manifest();
    const onChangeWorkflow = vi.fn();

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    await userEvent.selectOptions(
      screen.getByTestId("workflow-session-mode"),
      "persistent",
    );
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      runtime: {
        session: {
          type: "persistent",
        },
      },
    });
  });

  it("adds workflow-level prompt artifacts from workflow settings", async () => {
    const onChangeWorkflow = vi.fn();

    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId={null}
        tools={[]}
        prompts={[
          {
            prompt_name: "support-agent",
            alias: "prod",
            has_prompt: true,
          } as PromptInfo,
        ]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    await userEvent.click(screen.getByTestId("workflow-prompt-artifacts-add"));
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      artifacts: {
        prompts: {
          "support-agent": {
            registry_name: "support-agent",
            alias: "prod",
            managed_by: "mlflow_prompt_registry",
          },
        },
      },
    });
  });

  it("edits workflow-level prompt artifacts and only removes unused refs", async () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.artifacts = {
      prompts: {
        "support-agent": {
          registry_name: "support-agent",
          alias: "prod",
          managed_by: "mlflow_prompt_registry",
        },
        "unused-prompt": {
          registry_name: "refund-agent",
          alias: "staging",
          managed_by: "manual",
        },
      },
    };
    (m.nodes.agent as { instructions?: unknown }).instructions = {
      type: "mlflow_prompt",
      ref: "support-agent",
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        prompts={[
          {
            prompt_name: "support-agent",
            alias: "prod",
            has_prompt: true,
          } as PromptInfo,
          {
            prompt_name: "refund-agent",
            alias: "staging",
            has_prompt: true,
          } as PromptInfo,
        ]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    expect(
      screen.getByTestId("workflow-prompt-artifact-remove-support-agent"),
    ).toBeDisabled();

    fireEvent.change(
      screen.getByTestId("workflow-prompt-artifact-alias-unused-prompt"),
      {
        target: { value: "prod" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      artifacts: {
        prompts: {
          "support-agent": {
            registry_name: "support-agent",
            alias: "prod",
            managed_by: "mlflow_prompt_registry",
          },
          "unused-prompt": {
            registry_name: "refund-agent",
            alias: "prod",
            managed_by: "manual",
          },
        },
      },
    });

    await userEvent.click(
      screen.getByTestId("workflow-prompt-artifact-remove-unused-prompt"),
    );
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      artifacts: {
        prompts: {
          "support-agent": {
            registry_name: "support-agent",
            alias: "prod",
            managed_by: "mlflow_prompt_registry",
          },
        },
      },
    });
  });

  it("updates start trigger mode, event fields, cron fields, target alias, and enabled state", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () =>
        HttpResponse.json(
          envelope([
            {
              deployment_id: "DEP-prod",
              workflow_id: "wf",
              alias: "prod",
              version_id: "WFV-1",
              environment: "production",
              status: "active",
              deployed_by: "@ops",
              deployed_at: "2026-06-10T00:00:00Z",
            },
            {
              deployment_id: "DEP-staging",
              workflow_id: "wf",
              alias: "staging",
              version_id: "WFV-2",
              environment: "staging",
              status: "active",
              deployed_by: "@ops",
              deployed_at: "2026-06-09T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-cron-preview`, () =>
        HttpResponse.json(
          envelope({
            timezone: "UTC",
            expression: "0 9 * * *",
            fire_times: [],
          }),
        ),
      ),
    );
    const m = manifest();
    m.nodes.start = {
      id: "start",
      type: "start",
      outputs: { user_message: { type: "string" } },
    };
    const onChangeNode = vi.fn();
    const { rerender } = renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="start"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    await userEvent.selectOptions(
      screen.getByTestId("inspector-start-mode"),
      "event",
    );
    expect(onChangeNode).toHaveBeenCalledWith("start", {
      trigger: { mode: "event", event_name: "", alias: "prod", enabled: true },
    });

    m.nodes.start = {
      ...m.nodes.start,
      trigger: {
        mode: "event",
        event_name: "object.created",
        alias: "prod",
        enabled: true,
      },
    };
    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="start"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(
      await screen.findByText(
        "This event trigger targets active deployment alias prod.",
      ),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("inspector-start-event-name"), {
      target: { value: "minio.object.created" },
    });
    await userEvent.click(screen.getByTestId("inspector-start-alias-staging"));
    await userEvent.click(screen.getByLabelText("Enabled"));
    expect(onChangeNode).toHaveBeenCalledWith("start", {
      trigger: expect.objectContaining({ event_name: "minio.object.created" }),
    });
    expect(onChangeNode).toHaveBeenCalledWith("start", {
      trigger: expect.objectContaining({ alias: "staging" }),
    });
    expect(onChangeNode).toHaveBeenCalledWith("start", {
      trigger: expect.objectContaining({ enabled: false }),
    });

    await userEvent.selectOptions(
      screen.getByTestId("inspector-start-mode"),
      "cron",
    );
    expect(onChangeNode).toHaveBeenCalledWith("start", {
      trigger: {
        mode: "cron",
        cron: "0 9 * * *",
        timezone: "UTC",
        alias: "prod",
        enabled: true,
      },
    });

    m.nodes.start = {
      ...m.nodes.start,
      trigger: {
        mode: "cron",
        cron: "0 9 * * *",
        timezone: "UTC",
        alias: "prod",
        enabled: true,
      },
    };
    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="start"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("inspector-start-cron"), {
      target: { value: "*/5 * * * *" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("start", {
      trigger: expect.objectContaining({ cron: "*/5 * * * *" }),
    });

    await userEvent.selectOptions(
      screen.getByTestId("inspector-start-mode"),
      "manual",
    );
    expect(onChangeNode).toHaveBeenCalledWith("start", { trigger: null });
  });

  it("previews the next cron fire times for a cron Start trigger", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-cron-preview`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("expr")).toBe("0 9 * * *");
        expect(url.searchParams.get("tz")).toBe("UTC");
        return HttpResponse.json(
          envelope({
            timezone: "UTC",
            expression: "0 9 * * *",
            fire_times: ["2026-06-25T09:00:00", "2026-06-26T09:00:00"],
          }),
        );
      }),
    );
    const m = manifest();
    m.nodes.start = {
      id: "start",
      type: "start",
      outputs: { user_message: { type: "string" } },
      trigger: {
        mode: "cron",
        cron: "0 9 * * *",
        timezone: "UTC",
        alias: "prod",
        enabled: true,
      },
    };
    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="start"
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    const preview = await screen.findByTestId("inspector-start-cron-preview");
    const fires = await within(preview).findAllByTestId(
      "inspector-start-cron-fire-time",
    );
    expect(fires).toHaveLength(2);
    expect(fires[0]).toHaveTextContent("2026-06-25 09:00");
    expect(fires[1]).toHaveTextContent("2026-06-26 09:00");
    expect(
      within(preview).getByText("Times shown in UTC."),
    ).toBeInTheDocument();
  });

  it("shows a friendly message when the cron preview request fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/deployments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-cron-preview`, () =>
        HttpResponse.json(
          { error: { message: "invalid cron expression" } },
          { status: 400 },
        ),
      ),
    );
    const m = manifest();
    m.nodes.start = {
      id: "start",
      type: "start",
      outputs: { user_message: { type: "string" } },
      trigger: {
        mode: "cron",
        cron: "99 * * * *",
        timezone: "UTC",
        alias: "prod",
        enabled: true,
      },
    };
    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="start"
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(
      await screen.findByTestId("inspector-start-cron-preview-error"),
    ).toBeInTheDocument();
  });

  it("shows agent form with tool checkboxes and fires onChangeNode", async () => {
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[tool("lookup_policy"), tool("issue_refund", "write")]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByTestId("wf-inspector")).toHaveAttribute(
      "data-node-type",
      "agent",
    );
    await userEvent.click(screen.getByTestId("tools-add"));
    await userEvent.click(screen.getByTestId("tool-lookup_policy"));
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      tools: ["lookup_policy"],
      tool_constraints: {},
    });
  });

  it("switches agent instructions to registered-prompt mode", async () => {
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[]}
        prompts={[
          {
            prompt_name: "support-agent",
            alias: "prod",
            has_prompt: true,
          } as PromptInfo,
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    // Inline by default → the toggle flips the node to a prompt ref.
    await userEvent.click(screen.getByTestId("instructions-mode-prompt"));
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      instructions: { type: "mlflow_prompt", ref: "" },
    });
  });

  it("picking a registered prompt wires the node ref and artifacts.prompts", () => {
    const onChangeNode = vi.fn();
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    (m.nodes.agent as { instructions?: unknown }).instructions = {
      type: "mlflow_prompt",
      ref: "",
    };
    render(
      <Inspector
        manifest={m}
        selectedNodeId="agent"
        tools={[]}
        prompts={[
          {
            prompt_name: "support-agent",
            alias: "prod",
            has_prompt: true,
          } as PromptInfo,
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );
    fireEvent.change(screen.getByTestId("inspector-prompt-ref"), {
      target: { value: "support-agent" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      instructions: { type: "mlflow_prompt", ref: "support-agent" },
    });
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      artifacts: {
        prompts: {
          "support-agent": {
            registry_name: "support-agent",
            alias: "prod",
            managed_by: "mlflow_prompt_registry",
          },
        },
      },
    });
  });

  it("captures agent structured output schema and auto-adds a structured port", () => {
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    const schema = {
      type: "object",
      properties: {
        answer: { type: "string" },
      },
      required: ["answer"],
    };
    fireEvent.change(screen.getByTestId("inspector-agent-output-type"), {
      target: { value: JSON.stringify(schema, null, 2) },
    });
    fireEvent.blur(screen.getByTestId("inspector-agent-output-type"));

    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      output_type: schema,
      outputs: {
        final_output: { type: "string" },
        structured_output: { type: "structured" },
      },
    });
  });

  it("updates agent tool constraints", async () => {
    const onChangeNode = vi.fn();
    const m = manifest();
    m.nodes.agent = {
      ...m.nodes.agent,
      tools: ["lookup_policy"],
    };
    render(
      <Inspector
        manifest={m}
        selectedNodeId="agent"
        tools={[tool("lookup_policy")]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    await userEvent.selectOptions(
      screen.getByTestId("inspector-tool-constraint-lookup_policy"),
      "required_before_claim",
    );
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      tool_constraints: { lookup_policy: "required_before_claim" },
    });
  });

  it("picking an eval dataset wires the node ref and artifacts.eval_datasets", async () => {
    const onChangeNode = vi.fn();
    const onChangeWorkflow = vi.fn();
    const dataset = {
      dataset_id: "ED-1",
      name: "Support Eval",
      description: "",
      owner: "@test",
      tags: [],
      status: "active",
      version: 1,
      created_at: "2026-06-10T00:00:00Z",
      updated_at: "2026-06-10T00:00:00Z",
    } as EvalDataset;

    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[]}
        evalDatasets={[dataset]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    await userEvent.selectOptions(
      screen.getByTestId("inspector-agent-eval-dataset"),
      "ED-1",
    );
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      eval_dataset: "ED-1",
    });
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      artifacts: {
        eval_datasets: {
          "ED-1": {
            dataset_name: "Support Eval",
          },
        },
      },
    });
  });

  it("toggles agent skills and fires onChangeNode", async () => {
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[]}
        skills={[
          {
            skill_id: "SK-1",
            name: "tone",
            summary: "Keep it concise",
          } as Skill,
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByTestId("skills-add"));
    await userEvent.click(screen.getByTestId("skill-tone"));
    expect(onChangeNode).toHaveBeenCalledWith("agent", { skills: ["tone"] });
  });

  it("adds, edits, and removes agent handoffs", async () => {
    const onChangeNode = vi.fn();
    const m = manifest();
    m.nodes.billing = {
      id: "billing",
      type: "agent",
      name: "billing-agent",
      model: "inherit",
      instructions: { type: "inline", text: "Handle billing cases." },
      tools: [],
      inputs: { input: { type: "string" } },
      outputs: { final_output: { type: "string" } },
    };
    const { rerender } = render(
      <Inspector
        manifest={m}
        selectedNodeId="agent"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByTestId("handoffs-add"));
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      handoffs: [
        {
          target: "billing",
          description: "",
          condition: null,
          input_filter: null,
        },
      ],
    });

    m.nodes.agent = {
      ...m.nodes.agent,
      handoffs: [
        {
          target: "billing",
          description: "",
          condition: null,
          input_filter: null,
        },
      ],
    };
    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="agent"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("handoff-description-0"), {
      target: { value: "Escalate billing disputes" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      handoffs: [
        {
          target: "billing",
          description: "Escalate billing disputes",
          condition: null,
          input_filter: null,
        },
      ],
    });

    fireEvent.change(screen.getByTestId("handoff-condition-0"), {
      target: { value: "refund_total > 1000" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      handoffs: [
        {
          target: "billing",
          description: "",
          condition: "refund_total > 1000",
          input_filter: null,
        },
      ],
    });

    fireEvent.change(screen.getByTestId("handoff-input-filter-0"), {
      target: { value: "Forward only the refund summary" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      handoffs: [
        {
          target: "billing",
          description: "",
          condition: null,
          input_filter: "Forward only the refund summary",
        },
      ],
    });

    await userEvent.click(screen.getByTestId("handoff-remove-0"));
    expect(onChangeNode).toHaveBeenCalledWith("agent", { handoffs: [] });
  });

  it("disables handoff creation when no other agent targets exist", () => {
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("handoffs-add")).toBeDisabled();
    expect(
      screen.getByText("Add another Agent node to unlock delegation handoffs."),
    ).toBeInTheDocument();
  });

  it("shows workflow settings when nothing is selected", () => {
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByText("Workflow settings")).toBeInTheDocument();
  });

  it("updates workflow-level settings fields", () => {
    const onChangeWorkflow = vi.fn();
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Updated WF" },
    });
    fireEvent.change(screen.getByLabelText("Owner"), {
      target: { value: "@ops" },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Workflow description" },
    });
    expect(onChangeWorkflow).toHaveBeenCalledWith({ name: "Updated WF" });
    expect(onChangeWorkflow).toHaveBeenCalledWith({ owner: "@ops" });
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      description: "Workflow description",
    });
  });

  it("updates workflow runtime defaults", () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.runtime = {
      sdk: "openai-agents-python",
      sdk_version_policy: "runtime-pinned",
      compiler_version: "caliber-workflow-compiler-v1",
      default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
      session: { type: "none" },
      openai: {
        workflow_api: "chat_completions",
        parallel_tool_calls: "auto",
        prompt_cache_mode: "auto",
        prompt_cache_retention: "default",
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    expect(screen.getByText("openai-agents-python")).toBeInTheDocument();
    expect(screen.getByText("runtime-pinned")).toBeInTheDocument();
    expect(
      screen.getByText("caliber-workflow-compiler-v1"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("workflow-default-model-ref"), {
      target: { value: "gpt-4.1-mini" },
    });
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      runtime: {
        sdk: "openai-agents-python",
        sdk_version_policy: "runtime-pinned",
        compiler_version: "caliber-workflow-compiler-v1",
        default_model_ref: "gpt-4.1-mini",
        session: { type: "none" },
        openai: {
          workflow_api: "chat_completions",
          parallel_tool_calls: "auto",
          prompt_cache_mode: "auto",
          prompt_cache_retention: "default",
        },
      },
    });

    fireEvent.change(screen.getByTestId("workflow-openai-api"), {
      target: { value: "responses" },
    });
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      runtime: {
        sdk: "openai-agents-python",
        sdk_version_policy: "runtime-pinned",
        compiler_version: "caliber-workflow-compiler-v1",
        default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
        session: { type: "none" },
        openai: {
          workflow_api: "responses",
          parallel_tool_calls: "auto",
          prompt_cache_mode: "auto",
          prompt_cache_retention: "default",
        },
      },
    });

    fireEvent.change(
      screen.getByTestId("workflow-openai-prompt-cache-retention"),
      {
        target: { value: "24h" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      runtime: {
        sdk: "openai-agents-python",
        sdk_version_policy: "runtime-pinned",
        compiler_version: "caliber-workflow-compiler-v1",
        default_model_ref: "CALIBER_WORKFLOW_DEFAULT_MODEL",
        session: { type: "none" },
        openai: {
          workflow_api: "chat_completions",
          parallel_tool_calls: "auto",
          prompt_cache_mode: "auto",
          prompt_cache_retention: "24h",
        },
      },
    });
  });

  it("updates workflow-level MLflow settings", async () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.mlflow = {
      experiment_name: "caliber/support",
      trace_group_tags: {
        team: "ops",
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    fireEvent.change(screen.getByTestId("workflow-mlflow-experiment-name"), {
      target: { value: "caliber/escalations" },
    });
    expect(onChangeWorkflow).toHaveBeenCalledWith({
      mlflow: {
        experiment_name: "caliber/escalations",
        trace_group_tags: { team: "ops" },
      },
    });

    fireEvent.change(screen.getByTestId("workflow-mlflow-trace-group-tags"), {
      target: { value: "team=ops\nservice=support" },
    });
    fireEvent.blur(screen.getByTestId("workflow-mlflow-trace-group-tags"));

    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      mlflow: {
        experiment_name: "caliber/support",
        trace_group_tags: {
          team: "ops",
          service: "support",
        },
      },
    });
  });

  it("edits workflow-level registered tool bindings", async () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.nodes.agent.tools = ["lookup_policy"];
    m.tools = {
      lookup_policy: {
        registry_ref: "tool.lookup_policy.v1",
        version_constraint: ">=1.0,<2.0",
        requires_approval: false,
        secret_refs: ["POLICY_API_KEY"],
        timeout_seconds: 30,
        max_retries: 0,
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[tool("lookup_policy")]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    expect(screen.getByText("Tool bindings")).toBeInTheDocument();
    expect(screen.getByText("Used by agent")).toBeInTheDocument();
    expect(
      screen.getByTestId("workflow-tool-binding-remove-lookup-policy"),
    ).toBeDisabled();

    fireEvent.change(
      screen.getByTestId("workflow-tool-binding-registry-ref-lookup-policy"),
      {
        target: { value: "tool.lookup_policy.v2" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      tools: {
        lookup_policy: expect.objectContaining({
          registry_ref: "tool.lookup_policy.v2",
          version_constraint: ">=1.0,<2.0",
          requires_approval: false,
          secret_refs: ["POLICY_API_KEY"],
          timeout_seconds: 30,
          max_retries: 0,
        }),
      },
    });

    fireEvent.click(
      screen.getByTestId("workflow-tool-binding-approval-lookup-policy"),
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      tools: {
        lookup_policy: expect.objectContaining({
          registry_ref: "tool.lookup_policy.v1",
          requires_approval: true,
        }),
      },
    });
  });

  it("edits workflow-level MCP tool bindings", () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.tools = {
      customer_lookup: {
        type: "mcp_tool",
        server_id: "MCP-CRM",
        tool_name: "search_customers",
        tool_schema_version: "2026-06-01",
        side_effect_level: "read",
        requires_approval: false,
        timeout_seconds: 45,
        max_retries: 1,
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    fireEvent.change(
      screen.getByTestId("workflow-tool-binding-server-id-customer-lookup"),
      {
        target: { value: "MCP-SUPPORT" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      tools: {
        customer_lookup: expect.objectContaining({
          type: "mcp_tool",
          server_id: "MCP-SUPPORT",
          tool_name: "search_customers",
        }),
      },
    });

    fireEvent.change(
      screen.getByTestId("workflow-tool-binding-side-effect-customer-lookup"),
      {
        target: { value: "write" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      tools: {
        customer_lookup: expect.objectContaining({
          type: "mcp_tool",
          side_effect_level: "write",
          server_id: "MCP-CRM",
        }),
      },
    });
  });

  it("removes unused workflow-level tool bindings", async () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.tools = {
      orphan_binding: {
        registry_ref: "tool.orphan_binding.v1",
        version_constraint: "",
        requires_approval: false,
        secret_refs: [],
        timeout_seconds: null,
        max_retries: 0,
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    const removeButton = screen.getByTestId(
      "workflow-tool-binding-remove-orphan-binding",
    );
    expect(removeButton).not.toBeDisabled();

    await userEvent.click(removeButton);
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({ tools: {} });
  });

  it("adds workflow-level deploy gates and links eval dataset artifacts", async () => {
    const onChangeWorkflow = vi.fn();
    const dataset = {
      dataset_id: "ED-1",
      name: "Support Eval",
      description: "",
      owner: "@test",
      tags: [],
      status: "active",
      version: 1,
      created_at: "2026-06-10T00:00:00Z",
      updated_at: "2026-06-10T00:00:00Z",
    } as EvalDataset;

    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId={null}
        tools={[]}
        evalDatasets={[dataset]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    await userEvent.click(screen.getByTestId("workflow-deploy-gates-add"));
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      deploy_gates: {
        deploy_gate_1: {
          type: "deploy_gate",
          dataset_ref: "ED-1",
          required_for_aliases: ["prod"],
          thresholds: { min_pass_rate: 1 },
        },
      },
      artifacts: {
        eval_datasets: {
          "ED-1": {
            dataset_name: "Support Eval",
          },
        },
      },
    });
  });

  it("edits workflow-level deploy gates", () => {
    const onChangeWorkflow = vi.fn();
    const dataset = {
      dataset_id: "ED-1",
      name: "Support Eval",
      description: "",
      owner: "@test",
      tags: [],
      status: "active",
      version: 1,
      created_at: "2026-06-10T00:00:00Z",
      updated_at: "2026-06-10T00:00:00Z",
    } as EvalDataset;
    const m = manifest();
    m.artifacts = {
      eval_datasets: {
        "ED-1": {
          dataset_name: "Support Eval",
        },
      },
    };
    m.deploy_gates = {
      support_release: {
        type: "deploy_gate",
        dataset_ref: "ED-1",
        required_for_aliases: ["prod"],
        thresholds: { min_pass_rate: 1 },
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        evalDatasets={[dataset]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    fireEvent.change(
      screen.getByTestId("workflow-deploy-gate-aliases-support-release"),
      {
        target: { value: "prod, staging" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      deploy_gates: {
        support_release: expect.objectContaining({
          required_for_aliases: ["prod", "staging"],
        }),
      },
    });

    fireEvent.change(
      screen.getByTestId(
        "workflow-deploy-gate-threshold-min-overall-delta-support-release",
      ),
      {
        target: { value: "0.02" },
      },
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({
      deploy_gates: {
        support_release: expect.objectContaining({
          thresholds: {
            min_pass_rate: 1,
            min_overall_delta: 0.02,
          },
        }),
      },
    });
  });

  it("removes workflow-level deploy gates", async () => {
    const onChangeWorkflow = vi.fn();
    const m = manifest();
    m.deploy_gates = {
      support_release: {
        type: "deploy_gate",
        dataset_ref: "ED-1",
        required_for_aliases: ["prod"],
        thresholds: { min_pass_rate: 1 },
      },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId={null}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={onChangeWorkflow}
      />,
    );

    await userEvent.click(
      screen.getByTestId("workflow-deploy-gate-remove-support-release"),
    );
    expect(onChangeWorkflow).toHaveBeenLastCalledWith({ deploy_gates: {} });
  });

  it("shows file input settings and updates path", async () => {
    const m = manifest();
    m.nodes.file_input = {
      id: "file_input",
      type: "file_input",
      path: "",
      max_bytes: 200000,
      encoding: "utf-8",
      inputs: { path: { type: "string" } },
      outputs: { text: { type: "string" }, metadata: { type: "structured" } },
    };
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={m}
        selectedNodeId="file_input"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("inspector-file-path"), {
      target: { value: "/tmp/input.txt" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("file_input", {
      path: "/tmp/input.txt",
      file_ref: null,
    });
  });

  it("selects a content-pinned project file for a file input node", async () => {
    server.use(
      http.get(`${API_BASE}/projects/PRJ-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: [
              {
                file_id: "FILE-1",
                file_ref: "caliber://projects/PRJ-1/input/source.md",
                name: "source.md",
                kind: "input",
                relative_path: "source.md",
                media_type: "text/markdown",
                size_bytes: 12,
                sha256: "a".repeat(64),
                status: "attached",
                producer_node_id: null,
                created_at: null,
                immutable_ref: {
                  file_id: "FILE-1",
                  file_ref: "caliber://projects/PRJ-1/input/source.md",
                  sha256: "a".repeat(64),
                  name: "source.md",
                  size_bytes: 12,
                  media_type: "text/markdown",
                  object_version_id: null,
                },
              },
            ],
            directories: [],
            next_cursor: null,
          }),
        ),
      ),
    );
    const m = manifest();
    m.nodes.file_input = {
      id: "file_input",
      type: "file_input",
      path: "",
    };
    const onChangeNode = vi.fn();
    renderWithQuery(
      <Inspector
        manifest={m}
        projectId="PRJ-1"
        selectedNodeId="file_input"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    const selector = await screen.findByTestId("inspector-managed-file");
    await screen.findByRole("option", { name: /source\.md/ });
    await userEvent.selectOptions(selector, "FILE-1");
    expect(onChangeNode).toHaveBeenCalledWith("file_input", {
      file_ref: {
        file_id: "FILE-1",
        file_ref: "caliber://projects/PRJ-1/input/source.md",
        sha256: "a".repeat(64),
        name: "source.md",
        size_bytes: 12,
        media_type: "text/markdown",
        object_version_id: null,
      },
      path: "",
    });
  });

  it("renders backend field descriptions inline for inspector controls", () => {
    const m = manifest();
    m.nodes.file_input = {
      id: "file_input",
      type: "file_input",
      path: "",
      max_bytes: 200000,
      encoding: "utf-8",
      inputs: { path: { type: "string" } },
      outputs: { text: { type: "string" }, metadata: { type: "structured" } },
    };
    const componentSpec: WorkflowComponent = {
      type: "file_input",
      label: "File input",
      category: "Ingestion",
      description: "Reads a single local file into the workflow.",
      docs: [],
      default_inputs: {},
      default_outputs: {},
      fields: [
        {
          key: "path",
          label: "Path",
          type: "string",
          required: true,
          default: "",
          description: "Local filesystem path used by this node.",
          constraints: {},
          examples: ["/tmp/input.txt"],
        },
      ],
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId="file_input"
        tools={[]}
        componentSpec={componentSpec}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    const pathField = screen
      .getByTestId("inspector-file-path")
      .closest("label");
    expect(pathField).not.toBeNull();
    expect(pathField).toHaveTextContent("string");
    expect(pathField).toHaveTextContent("Required");
    expect(pathField).toHaveTextContent(
      "Local filesystem path used by this node.",
    );
    expect(screen.getByTestId("inspector-field-meta-path")).toHaveTextContent(
      "Example /tmp/input.txt",
    );
  });

  it("shows field-level setup guidance when a required component field is still incomplete", () => {
    const m = manifest();
    m.nodes.file_input = {
      id: "file_input",
      type: "file_input",
      path: "",
      max_bytes: 200000,
      encoding: "utf-8",
      inputs: { path: { type: "string" } },
      outputs: { text: { type: "string" }, metadata: { type: "structured" } },
    };
    const componentSpec: WorkflowComponent = {
      type: "file_input",
      label: "File input",
      category: "Ingestion",
      description: "Reads a single local file into the workflow.",
      docs: [],
      default_inputs: {},
      default_outputs: {},
      fields: [
        {
          key: "path",
          label: "Path",
          type: "string",
          required: true,
          default: "",
          description: "Local filesystem path used by this node.",
          constraints: {},
          examples: ["/tmp/input.txt"],
        },
      ],
      setup_checks: [
        {
          label: "Provide a file path",
          help: "Set the file path directly or map one into the node's path input.",
          kind: "non_empty_string",
          field: "path",
        },
      ],
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId="file_input"
        tools={[]}
        componentSpec={componentSpec}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("inspector-field-setup-path")).toHaveTextContent(
      "Provide a file path.",
    );
    expect(screen.getByTestId("inspector-field-setup-path")).toHaveTextContent(
      "Set the file path directly or map one into the node's path input.",
    );
  });

  it("shows field-level validation issues inline beside the affected control", () => {
    const m = manifest();
    m.nodes.file_input = {
      id: "file_input",
      type: "file_input",
      path: "",
      max_bytes: 200000,
      encoding: "utf-8",
      inputs: { path: { type: "string" } },
      outputs: { text: { type: "string" }, metadata: { type: "structured" } },
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId="file_input"
        tools={[]}
        validationReport={{
          valid: false,
          errors: [
            {
              code: "missing_file_path",
              path: "nodes.file_input.path",
              message: "Provide a file path before this node can run.",
              severity: "error",
            },
          ],
          warnings: [],
        }}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("inspector-field-issues-path")).toHaveTextContent(
      "Provide a file path before this node can run.",
    );
  });

  it("maps indexed handoff validation issues back onto the handoffs field", () => {
    const m = manifest();
    m.nodes.billing = {
      id: "billing",
      type: "agent",
      name: "billing-agent",
      model: "inherit",
      instructions: { type: "inline", text: "Handle billing cases." },
      tools: [],
      inputs: { input: { type: "string" } },
      outputs: { final_output: { type: "string" } },
    };
    m.nodes.agent = {
      ...m.nodes.agent,
      handoffs: [{ target: "agent" }],
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId="agent"
        tools={[]}
        validationReport={{
          valid: false,
          errors: [
            {
              code: "handoff_self_target",
              path: "nodes.agent.handoffs[0].target",
              message:
                "Handoff 1 on agent 'agent' targets the same agent. Pick a different specialist.",
              severity: "error",
            },
          ],
          warnings: [],
        }}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(
      screen.getByTestId("inspector-field-issues-handoffs"),
    ).toHaveTextContent(
      "Handoff 1 on agent 'agent' targets the same agent. Pick a different specialist.",
    );
  });

  it("highlights a targeted field when the editor focuses a validation issue", () => {
    const m = manifest();
    m.nodes.agent = {
      ...m.nodes.agent,
      tools: [],
    };

    render(
      <Inspector
        manifest={m}
        selectedNodeId="agent"
        focusFieldKey="tools"
        focusFieldSignal={1}
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("inspector-field-tools")).toHaveAttribute(
      "data-highlighted",
      "true",
    );
  });

  it("shows folder input settings and toggles recursion", async () => {
    const m = manifest();
    m.nodes.folder_input = {
      id: "folder_input",
      type: "folder_input",
      path: "",
      pattern: "**/*",
      recursive: true,
      max_files: 50,
      inputs: { path: { type: "string" } },
      outputs: { text: { type: "string" }, files: { type: "structured" } },
    };
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={m}
        selectedNodeId="folder_input"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByTestId("inspector-field-recursive")).toHaveAttribute(
      "data-workflow-field-key",
      "recursive",
    );
    await userEvent.click(screen.getByTestId("inspector-folder-recursive"));
    expect(onChangeNode).toHaveBeenCalledWith("folder_input", {
      recursive: false,
    });
  });

  it("updates input bucket node storage controls and previews matching objects", async () => {
    server.use(...objectStoreHandlers());
    const m = manifest();
    m.nodes.input_bucket = {
      id: "input_bucket",
      type: "input_bucket",
      bucket: "reports",
      prefix: "service/",
      recursive: true,
      max_files: 50,
      max_bytes_per_file: 100000,
      encoding: "utf-8",
      inputs: {},
      outputs: { text: { type: "string" }, files: { type: "structured" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="input_bucket"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText(/2 objects/i)).toBeInTheDocument();
    expect(screen.getByTestId("inspector-field-recursive")).toHaveAttribute(
      "data-workflow-field-key",
      "recursive",
    );
    await userEvent.selectOptions(
      screen.getByTestId("inspector-input-bucket"),
      "logs",
    );
    fireEvent.change(screen.getByTestId("inspector-input-bucket-prefix"), {
      target: { value: "service/2026/" },
    });
    await userEvent.click(screen.getByLabelText("Recursive"));
    fireEvent.change(screen.getByLabelText("Max objects"), {
      target: { value: "75" },
    });
    fireEvent.change(screen.getByLabelText("Bytes per object"), {
      target: { value: "2048" },
    });
    fireEvent.change(screen.getByTestId("inspector-input-bucket-encoding"), {
      target: { value: "utf-16" },
    });

    expect(onChangeNode).toHaveBeenCalledWith("input_bucket", {
      bucket: "logs",
    });
    expect(onChangeNode).toHaveBeenCalledWith("input_bucket", {
      prefix: "service/2026/",
    });
    expect(onChangeNode).toHaveBeenCalledWith("input_bucket", {
      recursive: false,
    });
    expect(onChangeNode).toHaveBeenCalledWith("input_bucket", {
      max_files: 75,
    });
    expect(onChangeNode).toHaveBeenCalledWith("input_bucket", {
      max_bytes_per_file: 2048,
    });
    expect(onChangeNode).toHaveBeenCalledWith("input_bucket", {
      encoding: "utf-16",
    });
  });

  it("updates output bucket and output folder settings", async () => {
    server.use(...objectStoreHandlers());
    const m = manifest();
    m.nodes.output_bucket = {
      id: "output_bucket",
      type: "output_bucket",
      bucket: "reports",
      prefix: "exports/",
      overwrite: true,
      inputs: { artifact: { type: "structured" } },
      outputs: {},
    };
    m.nodes.output_folder = {
      id: "output_folder",
      type: "output_folder",
      path: "/tmp/exports",
      overwrite: true,
      inputs: { artifact: { type: "structured" } },
      outputs: {},
    };
    const onChangeNode = vi.fn();
    const { rerender } = renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="output_bucket"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("link", { name: /Open in Object Store/i }),
    ).toHaveAttribute(
      "href",
      "/caliber/object-store?bucket=reports&prefix=exports%2F",
    );
    expect(screen.getByTestId("inspector-field-overwrite")).toHaveAttribute(
      "data-workflow-field-key",
      "overwrite",
    );
    await userEvent.selectOptions(
      screen.getByTestId("inspector-output-bucket"),
      "logs",
    );
    fireEvent.change(screen.getByTestId("inspector-output-bucket-prefix"), {
      target: { value: "runs/" },
    });
    await userEvent.click(screen.getByLabelText("Overwrite existing objects"));
    expect(onChangeNode).toHaveBeenCalledWith("output_bucket", {
      bucket: "logs",
    });
    expect(onChangeNode).toHaveBeenCalledWith("output_bucket", {
      prefix: "runs/",
    });
    expect(onChangeNode).toHaveBeenCalledWith("output_bucket", {
      overwrite: false,
    });

    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="output_folder"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByTestId("inspector-field-overwrite")).toHaveAttribute(
      "data-workflow-field-key",
      "overwrite",
    );
    fireEvent.change(screen.getByTestId("inspector-output-folder-path"), {
      target: { value: "/data/out" },
    });
    await userEvent.click(screen.getByLabelText("Overwrite existing files"));
    expect(onChangeNode).toHaveBeenCalledWith("output_folder", {
      path: "/data/out",
    });
    expect(onChangeNode).toHaveBeenCalledWith("output_folder", {
      overwrite: false,
    });
  });

  it("updates orchestration settings and execution policy", async () => {
    const m = manifest();
    m.nodes.wait_event = {
      id: "wait_event",
      type: "wait_for_event",
      event_name: "ticket.approved",
      correlation_key: "",
      inputs: { input: { type: "string" } },
      outputs: { output: { type: "string" } },
    };
    m.nodes.join_any = {
      id: "join_any",
      type: "join",
      mode: "all",
      inputs: { left: { type: "string" }, right: { type: "string" } },
      outputs: { output: { type: "string" }, merged: { type: "structured" } },
      execution_policy: { max_retries: 0, idempotent: false },
    };
    const onChangeNode = vi.fn();
    const { rerender } = render(
      <Inspector
        manifest={m}
        selectedNodeId="wait_event"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("inspector-wait-event-name"), {
      target: { value: "invoice.ready" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("wait_event", {
      event_name: "invoice.ready",
    });
    fireEvent.change(screen.getByTestId("inspector-wait-event-timeout"), {
      target: { value: "3600" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("wait_event", {
      timeout_seconds: 3600,
    });

    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="join_any"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(
      screen.getByTestId("inspector-field-execution_policy"),
    ).toHaveAttribute("data-workflow-field-key", "execution_policy");
    await userEvent.selectOptions(
      screen.getByDisplayValue("Wait for all inputs"),
      "any",
    );
    expect(onChangeNode).toHaveBeenCalledWith("join_any", { mode: "any" });
    await userEvent.click(screen.getByLabelText("Idempotent execution"));
    expect(onChangeNode).toHaveBeenCalledWith(
      "join_any",
      expect.objectContaining({
        execution_policy: expect.objectContaining({ idempotent: true }),
      }),
    );
  });

  it("shows MCP resource settings and updates server/tool configuration", async () => {
    const m = manifest();
    m.nodes.mcp_resource = {
      id: "mcp_resource",
      type: "mcp_resource",
      server_id: "",
      tool_name: "",
      timeout_seconds: 45,
      inputs: { input: { type: "string" } },
      outputs: { text: { type: "string" }, result: { type: "structured" } },
    };
    const onChangeNode = vi.fn();
    const { rerender } = render(
      <Inspector
        manifest={m}
        selectedNodeId="mcp_resource"
        tools={[]}
        mcpServers={[
          {
            server_id: "MCP-1",
            name: "Docs",
            description: "docs server",
            transport: "stdio",
            uri: "",
            command: "npx",
            args: ["docs"],
            env: {},
            headers: {},
            auth_type: "none",
            auth_config: {},
            tool_policies: {},
            icon: "book",
            status: "active",
            last_connected_at: null,
            connection_error: null,
            owner: "@qa",
            discovered_tools: [
              {
                name: "search_docs",
                description: "search docs",
                input_schema: { type: "object", properties: {} },
                output_schema: { type: "object", properties: {} },
              },
              {
                name: "open_ticket",
                description: "open ticket",
                input_schema: { type: "object", properties: {} },
                output_schema: { type: "object", properties: {} },
              },
            ],
            created_at: "x",
            updated_at: "x",
          },
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    await userEvent.selectOptions(
      screen.getByTestId("inspector-mcp-server"),
      "MCP-1",
    );
    expect(onChangeNode).toHaveBeenCalledWith("mcp_resource", {
      server_id: "MCP-1",
      tool_name: "search_docs",
    });
    m.nodes.mcp_resource = {
      ...m.nodes.mcp_resource,
      server_id: "MCP-1",
      tool_name: "search_docs",
    };
    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="mcp_resource"
        tools={[]}
        mcpServers={[
          {
            server_id: "MCP-1",
            name: "Docs",
            description: "docs server",
            transport: "stdio",
            uri: "",
            command: "npx",
            args: ["docs"],
            env: {},
            headers: {},
            auth_type: "none",
            auth_config: {},
            tool_policies: {},
            icon: "book",
            status: "active",
            last_connected_at: null,
            connection_error: null,
            owner: "@qa",
            discovered_tools: [
              {
                name: "search_docs",
                description: "search docs",
                input_schema: { type: "object", properties: {} },
                output_schema: { type: "object", properties: {} },
              },
              {
                name: "open_ticket",
                description: "open ticket",
                input_schema: { type: "object", properties: {} },
                output_schema: { type: "object", properties: {} },
              },
            ],
            created_at: "x",
            updated_at: "x",
          },
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    await userEvent.selectOptions(
      screen.getByTestId("inspector-mcp-tool"),
      "open_ticket",
    );
    expect(onChangeNode).toHaveBeenCalledWith("mcp_resource", {
      tool_name: "open_ticket",
    });
    fireEvent.change(screen.getByTestId("inspector-mcp-timeout"), {
      target: { value: "30" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("mcp_resource", {
      timeout_seconds: 30,
    });
  });

  it("shows direct tool settings and updates the selected binding", async () => {
    const m = manifest();
    m.nodes.tool_lookup = {
      id: "tool_lookup",
      type: "tool",
      tool_name: "",
      inputs: {
        input: { type: "string" },
        arguments: { type: "structured" },
      },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={m}
        selectedNodeId="tool_lookup"
        tools={[tool("lookup_policy"), tool("write_ticket", "write")]}
        mcpServers={[
          {
            server_id: "MCP-1",
            name: "Docs",
            description: "docs server",
            transport: "stdio",
            uri: "",
            command: "npx",
            args: ["docs"],
            env: {},
            headers: {},
            auth_type: "none",
            auth_config: {},
            tool_policies: {},
            icon: "book",
            status: "active",
            last_connected_at: null,
            connection_error: null,
            owner: "@qa",
            discovered_tools: [
              {
                name: "search_docs",
                description: "search docs",
                input_schema: { type: "object", properties: {} },
                output_schema: { type: "object", properties: {} },
              },
            ],
            created_at: "x",
            updated_at: "x",
          },
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(screen.getByTestId("inspector-tool-node-name")).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByTestId("inspector-tool-node-name"),
      "lookup_policy",
    );
    expect(onChangeNode).toHaveBeenCalledWith("tool_lookup", {
      tool_name: "lookup_policy",
    });
    await userEvent.selectOptions(
      screen.getByTestId("inspector-tool-node-name"),
      "mcp:Docs/search_docs",
    );
    expect(onChangeNode).toHaveBeenCalledWith("tool_lookup", {
      tool_name: "mcp:Docs/search_docs",
    });
  });

  it("shows python-code settings and updates code + timeout", async () => {
    const m = manifest();
    m.nodes.python = {
      id: "python",
      type: "python_code",
      code: 'return {"text": input}',
      timeout_seconds: 5,
      inputs: { input: { type: "string" }, context: { type: "structured" } },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={m}
        selectedNodeId="python"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("inspector-python-code"), {
      target: { value: 'return {"text": (input or "").upper()}' },
    });
    expect(onChangeNode).toHaveBeenCalledWith("python", {
      code: 'return {"text": (input or "").upper()}',
    });
    fireEvent.change(screen.getByTestId("inspector-python-timeout"), {
      target: { value: "12" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("python", {
      timeout_seconds: 12,
    });
  });

  it("shows template settings and updates template body, output format, and missing-variable mode", async () => {
    const m = manifest();
    m.nodes.template = {
      id: "template",
      type: "template",
      template: "Hello {{input}}",
      output_format: "text",
      missing_variable_mode: "preserve",
      inputs: { input: { type: "string" }, variables: { type: "structured" } },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={m}
        selectedNodeId="template"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("inspector-template-body"), {
      target: { value: '{"message":"{{input}}"}' },
    });
    expect(onChangeNode).toHaveBeenCalledWith("template", {
      template: '{"message":"{{input}}"}',
    });
    await userEvent.selectOptions(
      screen.getByTestId("inspector-template-output-format"),
      "json",
    );
    expect(onChangeNode).toHaveBeenCalledWith("template", {
      output_format: "json",
    });
    await userEvent.selectOptions(
      screen.getByTestId("inspector-template-missing-mode"),
      "error",
    );
    expect(onChangeNode).toHaveBeenCalledWith("template", {
      missing_variable_mode: "error",
    });
  });

  it("updates guardrail controls", async () => {
    const m = manifest();
    m.nodes.guard = {
      id: "guard",
      type: "guardrail",
      mode: "post_agent",
      on_failure: "block",
      checks: [{ non_empty_output: {} }],
      inputs: { response: { type: "string" } },
      outputs: { passthrough: { type: "string" } },
    };
    const onChangeNode = vi.fn();
    const { rerender } = render(
      <Inspector
        manifest={m}
        selectedNodeId="guard"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    await userEvent.selectOptions(
      screen.getByTestId("inspector-mode"),
      "pre_agent",
    );
    expect(
      screen.getByRole("option", { name: "Warn + Continue" }),
    ).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByTestId("inspector-on-failure"),
      "warn",
    );
    expect(
      screen.getByTestId("inspector-guardrail-max-retries"),
    ).toBeDisabled();
    await userEvent.selectOptions(
      screen.getByTestId("inspector-on-failure"),
      "block_retry",
    );
    rerender(
      <Inspector
        manifest={{
          ...m,
          nodes: {
            ...m.nodes,
            guard: {
              ...m.nodes.guard,
              on_failure: "block_retry",
            },
          },
        }}
        selectedNodeId="guard"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByTestId("inspector-guardrail-max-retries")).toBeEnabled();
    fireEvent.change(screen.getByTestId("inspector-guardrail-max-retries"), {
      target: { value: "2" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("guard", { mode: "pre_agent" });
    expect(onChangeNode).toHaveBeenCalledWith("guard", { on_failure: "warn" });
    expect(onChangeNode).toHaveBeenCalledWith("guard", {
      on_failure: "block_retry",
    });
    expect(onChangeNode).toHaveBeenCalledWith("guard", { max_retries: 2 });
  });

  it("shows inline node guidance and validation issues for incomplete nodes", () => {
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "",
      version_ids: [],
      retrieval_modes: ["dense"],
      top_k: 6,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        validationReport={{
          valid: false,
          errors: [
            {
              code: "missing_knowledge_target",
              path: "nodes.knowledge.knowledge_base_id",
              message: "Select a knowledge base or pinned version.",
              severity: "error",
            },
          ],
          warnings: [],
        }}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByTestId("inspector-node-guide")).toHaveTextContent(
      "knowledge base",
    );
    expect(
      screen.getByText("Select a knowledge base or pinned versions"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("inspector-node-issues")).toHaveTextContent(
      "Select a knowledge base or pinned version.",
    );
  });

  it("forwards router branch edits to onChangeNode", async () => {
    const m = manifest();
    m.nodes.router = {
      id: "router",
      type: "router",
      inputs: { decision: { type: "string" } },
      outputs: {},
      branches: [],
    };
    m.nodes.final = {
      id: "final",
      type: "output",
      inputs: { response: { type: "string" } },
    };
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={m}
        selectedNodeId="router"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByTestId("router-add-condition"));
    expect(onChangeNode).toHaveBeenCalledWith(
      "router",
      expect.objectContaining({ branches: expect.any(Array) }),
    );
  });

  it("renders human approval and note editors", async () => {
    const m = manifest();
    m.nodes.approval = {
      id: "approval",
      type: "human_approval",
      inputs: { request: { type: "string" } },
      outputs: { request: { type: "string" } },
    };
    m.nodes.note = {
      id: "note",
      type: "note",
      text: "Review this route.",
    };
    const onChangeNode = vi.fn();
    const { rerender } = render(
      <Inspector
        manifest={m}
        selectedNodeId="approval"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/Runtime approval records inherit this policy/i),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("inspector-approval-role"), {
      target: { value: "support.reviewer" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("approval", {
      required_role: "support.reviewer",
    });
    fireEvent.change(screen.getByTestId("inspector-approval-count"), {
      target: { value: "2" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("approval", {
      approval_count: 2,
    });
    await userEvent.selectOptions(
      screen.getByTestId("inspector-approval-timeout"),
      "escalate",
    );
    expect(onChangeNode).toHaveBeenCalledWith("approval", {
      timeout_behavior: "escalate",
    });

    rerender(
      <Inspector
        manifest={m}
        selectedNodeId="note"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    await userEvent.clear(screen.getByTestId("inspector-note-text"));
    await userEvent.type(
      screen.getByTestId("inspector-note-text"),
      "Updated note",
    );
    expect(
      onChangeNode.mock.calls.some(
        ([id, patch]) =>
          id === "note" &&
          typeof (patch as { text?: unknown }).text === "string",
      ),
    ).toBe(true);
  });

  it("shows delete action for mutable nodes and MCP tool toggles", async () => {
    const onDeleteNode = vi.fn();
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={manifest()}
        selectedNodeId="agent"
        tools={[tool("lookup_policy")]}
        mcpServers={[
          {
            server_id: "MCP-1",
            name: "Docs",
            description: "docs server",
            transport: "stdio",
            uri: "",
            command: "npx",
            args: ["docs"],
            env: {},
            headers: {},
            auth_type: "none",
            auth_config: {},
            tool_policies: {},
            icon: "book",
            status: "active",
            last_connected_at: null,
            connection_error: null,
            owner: "@qa",
            discovered_tools: [
              {
                name: "search_docs",
                description: "search docs",
                input_schema: { type: "object", properties: {} },
                output_schema: { type: "object", properties: {} },
              },
            ],
            created_at: "x",
            updated_at: "x",
          },
        ]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
        onDeleteNode={onDeleteNode}
      />,
    );
    await userEvent.click(screen.getByTestId("inspector-delete"));
    expect(onDeleteNode).toHaveBeenCalledWith("agent");
    await userEvent.click(screen.getByTestId("tools-add"));
    await userEvent.click(screen.getByTestId("mcp-tool-mcp:Docs/search_docs"));
    expect(onChangeNode).toHaveBeenCalledWith("agent", {
      tools: ["mcp:Docs/search_docs"],
      tool_constraints: {},
    });
  });

  it("configures knowledge query nodes with KB versions, AGE retrieval, and graph overrides", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: ["age_graph"],
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
        answer: { type: "string" },
        result: { type: "structured" },
      },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("option", { name: "Contracts KB" }),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("knowledge-retrieval-modes-help"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("knowledge-versions-add"));
    await userEvent.click(
      await screen.findByTestId("knowledge-versions-option-KBV-1"),
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      version_ids: ["KBV-1"],
    });

    await userEvent.click(screen.getByTestId("knowledge-modes-add"));
    await userEvent.click(
      screen.getByTestId("knowledge-modes-option-graph_hybrid"),
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      retrieval_modes: ["age_graph", "graph_hybrid"],
    });

    fireEvent.change(screen.getByTestId("inspector-knowledge-top-k"), {
      target: { value: "9" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", { top_k: 9 });

    fireEvent.change(screen.getByTestId("inspector-knowledge-chat-model"), {
      target: { value: "gpt-4.1-mini" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      chat_model: "gpt-4.1-mini",
    });

    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-strength"),
      "aggressive",
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      graph_overrides: { retrieval_strength: "aggressive" },
    });

    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-age-seed-mode"),
      "query_text_only",
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      graph_overrides: { age_seed_mode: "query_text_only" },
    });

    await userEvent.click(screen.getByLabelText("Strict AGE retrieval"));
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      graph_overrides: { strict_age_retrieval: true },
    });

    fireEvent.change(
      screen.getByTestId("inspector-knowledge-age-dense-weight"),
      {
        target: { value: "0.15" },
      },
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      graph_overrides: { age_dense_rerank_weight: 0.15 },
    });
  });

  it("offers retrieval presets for knowledge query nodes including an AGE-ready shortcut", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: ["dense"],
      top_k: 6,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText("AGE ready on v1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "AGE graph" }));
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      retrieval_modes: ["age_graph"],
    });
  });

  it("applies shared graph query profiles to knowledge query nodes", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: ["graph_hybrid"],
      top_k: 6,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText("Graph query profiles")).toBeInTheDocument();
    await userEvent.click(
      await screen.findByTestId("knowledge-query-preset-age_strict"),
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      retrieval_modes: ["age_graph"],
      graph_overrides: {
        retrieval_strength: "aggressive",
        minimum_relationship_weight: 1,
        age_seed_mode: "query_entities_and_text",
        age_traversal_hops: 2,
        age_candidate_pool_size: 40,
        age_dense_rerank_weight: 0.2,
        strict_age_retrieval: true,
      },
    });
  });

  it("switches legacy dense knowledge-query nodes back to KB default when a corpus is selected", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(
          envelope({
            chunking_strategies: [],
            embedding_models: [],
            retrieval_modes: [
              {
                id: "dense",
                name: "Dense retrieval",
                description: "Vector search",
                defaults: {},
                tags: [],
              },
              {
                id: "graph_hybrid",
                name: "Graph hybrid",
                description: "Graph + vector",
                defaults: {},
                tags: [],
              },
              {
                id: "age_graph",
                name: "Apache AGE",
                description: "AGE traversal",
                defaults: {},
                tags: [],
              },
            ],
            graph_extractors: [],
            graph_output_targets: [],
            graph_retrieval_strengths: [],
            graph_age_seed_modes: [],
            graph_entity_types: [],
            graph_query_presets: [
              {
                id: "hybrid_balanced",
                label: "Balanced GraphRAG",
                eyebrow: "Portable",
                description: "Balanced local graph retrieval.",
                badges: ["Local graph", "1-hop", "Balanced"],
                retrieval_mode: "graph_hybrid",
                patch: {
                  retrieval_strength: "balanced",
                  minimum_relationship_weight: 1,
                  age_traversal_hops: 1,
                },
                recommended: false,
                age_required: false,
              },
              {
                id: "age_native",
                label: "AGE-native retrieval",
                eyebrow: "Graph-first",
                description: "Graph-first AGE retrieval.",
                badges: ["AGE primary", "2-hop", "Graph-first"],
                retrieval_mode: "age_graph",
                patch: {
                  retrieval_strength: "aggressive",
                  minimum_relationship_weight: 1,
                  age_seed_mode: "query_entities_and_text",
                  age_traversal_hops: 2,
                  age_candidate_pool_size: 40,
                  age_dense_rerank_weight: 0.2,
                },
                recommended: true,
                age_required: true,
              },
              {
                id: "age_strict",
                label: "Strict AGE only",
                eyebrow: "Locked",
                description: "AGE-only retrieval with no fallback.",
                badges: ["AGE primary", "Strict", "No fallback"],
                retrieval_mode: "age_graph",
                patch: {
                  retrieval_strength: "aggressive",
                  minimum_relationship_weight: 1,
                  age_seed_mode: "query_entities_and_text",
                  age_traversal_hops: 2,
                  age_candidate_pool_size: 40,
                  age_dense_rerank_weight: 0.2,
                  strict_age_retrieval: true,
                },
                recommended: false,
                age_required: true,
              },
            ],
            default_graph_config: {
              extractor_backend: "heuristic",
              spacy_model: null,
              max_entities_per_chunk: 12,
              entity_types: [],
              minimum_entity_mentions: 1,
              minimum_relationship_weight: 1,
              default_retrieval_mode: "graph_hybrid",
              retrieval_strength: "balanced",
              output_target: "object_store",
              age_seed_mode: "entity_then_text",
              age_traversal_hops: 1,
              age_candidate_pool_size: 24,
              age_dense_rerank_weight: 0.35,
            },
            age_enabled: true,
            age_graph_name: "knowledge_graph",
            age_unavailable_reason: null,
            reserved_output_prefix: "knowledge/",
          }),
        ),
      ),
      http.get(`${KB}`, () =>
        HttpResponse.json(
          envelope([
            {
              knowledge_base_id: "KB-1",
              project_id: null,
              visibility: "user",
              name: "Contracts KB",
              description: "",
              owner: "@test",
              status: "active",
              source_bucket: "docs",
              source_manifest: [],
              source_fingerprint: "fp-1",
              active_version_id: "KBV-7",
              last_run_id: null,
              last_run_status: null,
              last_run_completed_at: null,
              created_at: "2026-06-10T00:00:00Z",
              updated_at: "2026-06-10T00:00:00Z",
              active_version_summary: {
                knowledge_base_version_id: "KBV-7",
                version_number: 7,
                status: "completed",
                chunking_strategy: "recursive_character",
                embedding_model: "BAAI/bge-base-en-v1.5",
                graph_extractor: "heuristic",
                graph_target: "object_store_and_age",
                default_retrieval_mode: "age_graph",
                retrieval_strength: "balanced",
                age_sync_status: "synced",
                age_ready: true,
                age_graph_name: "knowledge_graph",
                chunk_count: 42,
                entity_count: 18,
                relationship_count: 27,
                created_at: "2026-06-10T00:00:00Z",
                completed_at: "2026-06-10T00:02:00Z",
              },
            },
          ]),
        ),
      ),
    );
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "",
      version_ids: [],
      retrieval_modes: ["dense"],
      top_k: 6,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("option", { name: "Contracts KB" }),
    ).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-base"),
      "KB-1",
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: [],
    });
  });

  it("pins the latest synced version when AGE retrieval is requested before the active version is ready", async () => {
    server.use(
      http.get(`${KB}`, () =>
        HttpResponse.json(
          envelope([
            {
              knowledge_base_id: "KB-1",
              project_id: null,
              visibility: "user",
              name: "Contracts KB",
              description: "",
              owner: "@test",
              status: "active",
              source_bucket: "docs",
              source_manifest: [],
              source_fingerprint: "fp-1",
              active_version_id: "KBV-2",
              last_run_id: null,
              last_run_status: "processing",
              last_run_completed_at: null,
              created_at: "2026-06-10T00:00:00Z",
              updated_at: "2026-06-10T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(`${KB}/:knowledgeBaseId/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              knowledge_base_version_id: "KBV-2",
              knowledge_base_id: "KB-1",
              version_number: 2,
              status: "processing",
              chunking_strategy: "recursive_character",
              chunking_config: {},
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
                age_traversal_hops: 2,
                age_candidate_pool_size: 24,
                age_dense_rerank_weight: 0.35,
              },
              embedding_provider: "huggingface",
              embedding_model: "BAAI/bge-base-en-v1.5",
              embedding_dimension: 768,
              source_manifest: [],
              source_fingerprint: "fp-2",
              output_bucket: "docs",
              output_prefix: "knowledge/contracts/v2/",
              chunks_uri: null,
              entities_uri: null,
              relationships_uri: null,
              graph_uri: null,
              manifest_uri: null,
              logs_uri: null,
              stats_uri: null,
              summary: { age_sync_status: "processing" },
              error_summary: null,
              created_by: "@test",
              created_at: "2026-06-11T00:00:00Z",
              completed_at: null,
            },
            {
              knowledge_base_version_id: "KBV-1",
              knowledge_base_id: "KB-1",
              version_number: 1,
              status: "completed",
              chunking_strategy: "recursive_character",
              chunking_config: {},
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
              },
              embedding_provider: "huggingface",
              embedding_model: "BAAI/bge-base-en-v1.5",
              embedding_dimension: 768,
              source_manifest: [],
              source_fingerprint: "fp-1",
              output_bucket: "docs",
              output_prefix: "knowledge/contracts/v1/",
              chunks_uri: null,
              entities_uri: null,
              relationships_uri: null,
              graph_uri: null,
              manifest_uri: null,
              logs_uri: null,
              stats_uri: null,
              summary: { age_sync_status: "synced" },
              error_summary: null,
              created_by: "@test",
              created_at: "2026-06-10T00:00:00Z",
              completed_at: "2026-06-10T00:02:00Z",
            },
          ]),
        ),
      ),
    );
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: ["dense"],
      top_k: 6,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText("AGE ready on v1")).toBeInTheDocument();
    expect(
      screen.getByText(/Choosing AGE graph below will pin v1 automatically/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "AGE graph" }));
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      version_ids: ["KBV-1"],
      retrieval_modes: ["age_graph"],
    });
  });

  it("keeps AGE tuning enabled when the node follows an AGE-backed KB default", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: [],
      top_k: 6,
      graph_overrides: null,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText("Apache AGE")).toBeInTheDocument();
    const seedMode = screen.getByTestId("inspector-knowledge-age-seed-mode");
    expect(seedMode).not.toBeDisabled();

    await userEvent.selectOptions(seedMode, "query_entities_and_text");
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      graph_overrides: {
        age_seed_mode: "query_entities_and_text",
      },
    });
  });

  it("clears pinned versions when changing the selected knowledge base", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "",
      version_ids: ["KBV-stale"],
      retrieval_modes: ["graph_hybrid"],
      top_k: 6,
      inputs: { question: { type: "string" } },
      outputs: { answer: { type: "string" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="knowledge"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("option", { name: "Contracts KB" }),
    ).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-base"),
      "KB-1",
    );
    expect(onChangeNode).toHaveBeenCalledWith("knowledge", {
      knowledge_base_id: "KB-1",
      version_ids: [],
    });
  });

  it("configures knowledge build nodes with launch controls and KB defaults", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.build = {
      id: "build",
      type: "knowledge_build",
      knowledge_base_id: "KB-1",
      chunking_strategy: "recursive_character",
      embedding_model: "BAAI/bge-base-en-v1.5",
      chunking_config: {},
      graph_config: null,
      activate_when_complete: false,
      wait_for_completion: false,
      wait_timeout_seconds: 300,
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
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="build"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText("Current KB profile")).toBeInTheDocument();
    expect(await screen.findByText("Active v1")).toBeInTheDocument();
    expect(await screen.findByText("recursive_character")).toBeInTheDocument();
    expect(
      await screen.findByText("BAAI/bge-base-en-v1.5"),
    ).toBeInTheDocument();

    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-build-base"),
      "KB-2",
    );
    expect(onChangeNode).toHaveBeenCalledWith("build", {
      knowledge_base_id: "KB-2",
    });

    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-build-chunker"),
      "semantic",
    );
    expect(onChangeNode).toHaveBeenCalledWith("build", {
      chunking_strategy: "semantic",
    });

    await userEvent.selectOptions(
      screen.getByTestId("inspector-knowledge-build-embedding"),
      "intfloat/e5-large-v2",
    );
    expect(onChangeNode).toHaveBeenCalledWith("build", {
      embedding_model: "intfloat/e5-large-v2",
    });

    await userEvent.click(screen.getByTestId("inspector-knowledge-build-wait"));
    expect(onChangeNode).toHaveBeenCalledWith("build", {
      wait_for_completion: true,
    });

    fireEvent.change(screen.getByTestId("inspector-knowledge-build-timeout"), {
      target: { value: "900" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("build", {
      wait_timeout_seconds: 900,
    });

    await userEvent.click(
      screen.getByTestId("inspector-knowledge-build-activate"),
    );
    expect(onChangeNode).toHaveBeenCalledWith("build", {
      activate_when_complete: true,
    });
  });

  it("surfaces blocked local embedding runtimes in knowledge build nodes", async () => {
    const embeddingBlockedReason =
      "Local Hugging Face embedding builds are blocked because the current runtime includes flagged dependencies: torch 2.12.0 (CVE-2025-3000). Set CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true only if you explicitly accept the risk for this deployment.";

    server.use(
      ...knowledgeHandlers({
        embeddingUnavailableReason: embeddingBlockedReason,
      }),
    );
    const m = manifest();
    m.nodes.build = {
      id: "build",
      type: "knowledge_build",
      knowledge_base_id: "KB-1",
      chunking_strategy: "recursive_character",
      embedding_model: "BAAI/bge-base-en-v1.5",
      chunking_config: {},
      graph_config: null,
      activate_when_complete: false,
      wait_for_completion: false,
      wait_timeout_seconds: 300,
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
    };

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="build"
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(await screen.findByText(embeddingBlockedReason)).toBeInTheDocument();
    expect(
      screen.getByTestId("inspector-knowledge-build-embedding"),
    ).toBeDisabled();
    expect(
      screen.getByRole("option", {
        name: "BAAI / bge-base-en-v1.5 (Blocked)",
      }),
    ).toBeDisabled();
  });

  it("configures external app nodes with a callable entrypoint", async () => {
    const m = manifest();
    m.nodes.external = {
      id: "external",
      type: "external_app",
      entrypoint: "",
      inputs: {
        input: { type: "string" },
        context: { type: "structured" },
      },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };
    const onChangeNode = vi.fn();

    render(
      <Inspector
        manifest={m}
        selectedNodeId="external"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("inspector-external-entrypoint"), {
      target: { value: "support.ticketing:handle_request" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("external", {
      entrypoint: "support.ticketing:handle_request",
    });
  });

  it("lets orchestration nodes target executable non-agent nodes", async () => {
    server.use(...knowledgeHandlers());
    const m = manifest();
    m.nodes.python = {
      id: "python",
      type: "python_code",
      code: 'return {"text": input or run_input}',
      inputs: { input: { type: "string" } },
      outputs: { text: { type: "string" }, result: { type: "structured" } },
    };
    m.nodes.knowledge = {
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "KB-1",
      version_ids: [],
      retrieval_modes: ["dense"],
      top_k: 4,
      inputs: { question: { type: "string" } },
      outputs: { text: { type: "string" }, result: { type: "structured" } },
    };
    m.nodes.mcp = {
      id: "mcp",
      type: "mcp_resource",
      server_id: "MCP-1",
      tool_name: "search_docs",
      inputs: { input: { type: "string" } },
      outputs: { text: { type: "string" }, result: { type: "structured" } },
    };
    m.nodes.tool_lookup = {
      id: "tool_lookup",
      type: "tool",
      tool_name: "lookup_policy",
      inputs: {
        input: { type: "string" },
        arguments: { type: "structured" },
      },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };
    m.nodes.template = {
      id: "template",
      type: "template",
      template: '{"message":"{{input}}"}',
      output_format: "json",
      missing_variable_mode: "preserve",
      inputs: { input: { type: "string" }, variables: { type: "structured" } },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };
    m.nodes.external = {
      id: "external",
      type: "external_app",
      entrypoint: "support.ticketing:handle_request",
      inputs: { input: { type: "string" } },
      outputs: { text: { type: "string" }, result: { type: "structured" } },
    };
    m.nodes.subflow = {
      id: "subflow",
      type: "subworkflow",
      workflow_id: "wf-child",
      alias: "prod",
      timeout_seconds: 45,
      inputs: { input: { type: "string" } },
      outputs: { output: { type: "string" }, result: { type: "structured" } },
    };
    m.nodes.wait_event = {
      id: "wait_event",
      type: "wait_for_event",
      event_name: "ticket.approved",
      inputs: { input: { type: "string" } },
      outputs: { output: { type: "string" } },
    };
    m.nodes.for_each = {
      id: "for_each",
      type: "for_each",
      target_node_id: "agent",
      inputs: { items: { type: "structured" } },
      outputs: { text: { type: "string" }, results: { type: "structured" } },
    };
    m.nodes.loop = {
      id: "loop",
      type: "loop",
      target_node_id: "agent",
      max_iterations: 5,
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
    };
    m.nodes.boundary = {
      id: "boundary",
      type: "error_boundary",
      target_node_id: "agent",
      compensate_with: "python",
      fallback_text: "fallback",
      inputs: { input: { type: "string" } },
      outputs: { output: { type: "string" }, error: { type: "structured" } },
    };
    const onChangeNode = vi.fn();

    const firstRender = renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="for_each"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("option", { name: "python" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "knowledge" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "tool_lookup" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "mcp" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "template" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "external" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: "wait_event" }),
    ).not.toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByTestId("inspector-for-each-target"),
      "python",
    );
    expect(onChangeNode).toHaveBeenCalledWith("for_each", {
      target_node_id: "python",
    });

    firstRender.unmount();

    const loopRender = renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="loop"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    await userEvent.selectOptions(
      screen.getByTestId("inspector-loop-target"),
      "template",
    );
    expect(onChangeNode).toHaveBeenCalledWith("loop", {
      target_node_id: "template",
    });
    fireEvent.change(screen.getByTestId("inspector-loop-max-iterations"), {
      target: { value: "7" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("loop", {
      max_iterations: 7,
    });
    fireEvent.change(screen.getByTestId("inspector-loop-stop-condition"), {
      target: { value: "state.done" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("loop", {
      stop_condition: "state.done",
    });

    loopRender.unmount();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="boundary"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    await userEvent.selectOptions(
      screen.getByTestId("inspector-error-boundary-target"),
      "knowledge",
    );
    expect(onChangeNode).toHaveBeenCalledWith("boundary", {
      target_node_id: "knowledge",
    });
    await userEvent.selectOptions(
      screen.getByTestId("inspector-error-boundary-compensate"),
      "external",
    );
    expect(onChangeNode).toHaveBeenCalledWith("boundary", {
      compensate_with: "external",
    });
  });

  it("guides subworkflow selection and active deployment aliases", async () => {
    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(
          envelope([
            {
              workflow_id: "wf",
              name: "Parent Workflow",
              description: "Current workflow",
              owner: "@test",
              status: "active",
              default_experiment_id: null,
              created_at: "2026-06-10T00:00:00Z",
              updated_at: "2026-06-10T00:00:00Z",
            },
            {
              workflow_id: "wf-child",
              name: "Child Workflow",
              description: "Published child",
              owner: "@test",
              status: "active",
              default_experiment_id: null,
              created_at: "2026-06-10T00:00:00Z",
              updated_at: "2026-06-10T00:00:00Z",
            },
            {
              workflow_id: "wf-child-2",
              name: "Summaries",
              description: "Alternate child",
              owner: "@test",
              status: "paused",
              default_experiment_id: null,
              created_at: "2026-06-10T00:00:00Z",
              updated_at: "2026-06-10T00:00:00Z",
            },
          ]),
        ),
      ),
      http.get(
        `${API_BASE}/workflows/:workflowId/deployments`,
        ({ params }) => {
          if (params.workflowId === "wf-child") {
            return HttpResponse.json(
              envelope([
                {
                  deployment_id: "DEP-prod-1",
                  workflow_id: "wf-child",
                  alias: "prod",
                  version_id: "WVF-101",
                  environment: "production",
                  status: "active",
                  deployed_by: "@test",
                  deployed_at: "2026-06-10T01:00:00Z",
                },
                {
                  deployment_id: "DEP-staging-1",
                  workflow_id: "wf-child",
                  alias: "staging",
                  version_id: "WVF-100",
                  environment: "staging",
                  status: "active",
                  deployed_by: "@test",
                  deployed_at: "2026-06-09T01:00:00Z",
                },
              ]),
            );
          }
          if (params.workflowId === "wf-child-2") {
            return HttpResponse.json(
              envelope([
                {
                  deployment_id: "DEP-prod-2",
                  workflow_id: "wf-child-2",
                  alias: "prod",
                  version_id: "WVF-202",
                  environment: "production",
                  status: "active",
                  deployed_by: "@test",
                  deployed_at: "2026-06-11T01:00:00Z",
                },
              ]),
            );
          }
          return HttpResponse.json(envelope([]));
        },
      ),
      http.get(`${API_BASE}/workflows/:workflowId/versions`, ({ params }) => {
        if (params.workflowId === "wf-child") {
          return HttpResponse.json(
            envelope([
              {
                version_id: "WVF-100",
                workflow_id: "wf-child",
                version_number: 2,
                status: "published",
                manifest: {
                  schema_version: 1,
                  workflow_id: "wf-child",
                  name: "Child Workflow",
                  nodes: {
                    start: {
                      id: "start",
                      type: "start",
                      trigger: {
                        mode: "event",
                        event_name: "ticket.approved",
                        alias: "prod",
                        enabled: true,
                      },
                      outputs: { user_message: { type: "string" } },
                    },
                    agent: {
                      id: "agent",
                      type: "agent",
                      name: "Child agent",
                      model: "inherit",
                      instructions: {
                        type: "inline",
                        text: "Summarize the approval context.",
                      },
                      tools: [],
                      inputs: { input: { type: "string" } },
                      outputs: { final_output: { type: "string" } },
                    },
                    final: {
                      id: "final",
                      type: "output",
                      inputs: { response: { type: "string" } },
                    },
                  },
                  edges: [
                    {
                      id: "e1",
                      from: "start",
                      to: "agent",
                      map: { user_message: "input" },
                    },
                    {
                      id: "e2",
                      from: "agent",
                      to: "final",
                      map: { final_output: "response" },
                    },
                  ],
                  tools: {},
                },
                manifest_hash: "hash-child-2",
                compiler_version: "compiler",
                compiled_artifact_uri: "s3://compiled/wf-child/2.py",
                compiled_bundle: null,
                validation_report: { valid: true, errors: [], warnings: [] },
                created_by: "@test",
                created_at: "2026-06-09T00:00:00Z",
                published_by: "@test",
                published_at: "2026-06-09T00:30:00Z",
              },
              {
                version_id: "WVF-101",
                workflow_id: "wf-child",
                version_number: 3,
                status: "published",
                manifest: {
                  schema_version: 1,
                  workflow_id: "wf-child",
                  name: "Child Workflow",
                  nodes: {
                    start: {
                      id: "start",
                      type: "start",
                      trigger: {
                        mode: "event",
                        event_name: "ticket.approved",
                        alias: "prod",
                        enabled: true,
                      },
                      outputs: { user_message: { type: "string" } },
                    },
                    router: {
                      id: "router",
                      type: "router",
                      inputs: { decision: { type: "string" } },
                      outputs: {},
                      branches: [],
                    },
                    agent: {
                      id: "agent",
                      type: "agent",
                      name: "Child agent",
                      model: "inherit",
                      instructions: {
                        type: "inline",
                        text: "Summarize the approval context.",
                      },
                      tools: [],
                      inputs: { input: { type: "string" } },
                      outputs: { final_output: { type: "string" } },
                    },
                    review_note: {
                      id: "review_note",
                      type: "note",
                      text: "Escalate only when the approval payload is incomplete.",
                    },
                    final: {
                      id: "final",
                      type: "output",
                      inputs: { response: { type: "string" } },
                    },
                  },
                  edges: [
                    {
                      id: "e1",
                      from: "start",
                      to: "agent",
                      map: { user_message: "input" },
                    },
                    {
                      id: "e2",
                      from: "agent",
                      to: "final",
                      map: { final_output: "response" },
                    },
                  ],
                  tools: {},
                },
                manifest_hash: "hash-child-3",
                compiler_version: "compiler",
                compiled_artifact_uri: "s3://compiled/wf-child/3.py",
                compiled_bundle: null,
                validation_report: { valid: true, errors: [], warnings: [] },
                created_by: "@test",
                created_at: "2026-06-10T00:00:00Z",
                published_by: "@test",
                published_at: "2026-06-10T00:30:00Z",
              },
            ]),
          );
        }
        if (params.workflowId === "wf-child-2") {
          return HttpResponse.json(
            envelope([
              {
                version_id: "WVF-202",
                workflow_id: "wf-child-2",
                version_number: 7,
                status: "draft",
                manifest: {
                  schema_version: 1,
                  workflow_id: "wf-child-2",
                  name: "Summaries",
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
                },
                manifest_hash: "hash-child-7",
                compiler_version: "compiler",
                compiled_artifact_uri: null,
                compiled_bundle: null,
                validation_report: {
                  valid: false,
                  errors: [],
                  warnings: [
                    {
                      code: "draft_warning",
                      path: "nodes.final",
                      message: "Needs review",
                      severity: "warning",
                    },
                  ],
                },
                created_by: "@test",
                created_at: "2026-06-11T00:00:00Z",
                published_by: null,
                published_at: null,
              },
            ]),
          );
        }
        return HttpResponse.json(envelope([]));
      }),
    );
    const m = manifest();
    m.nodes.subflow = {
      id: "subflow",
      type: "subworkflow",
      workflow_id: "wf-child",
      alias: "prod",
      timeout_seconds: 45,
      inputs: { input: { type: "string" } },
      outputs: { output: { type: "string" }, result: { type: "structured" } },
    };
    const onChangeNode = vi.fn();

    renderWithQuery(
      <Inspector
        manifest={m}
        selectedNodeId="subflow"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    const workflowShortcut = await screen.findByTestId(
      "inspector-subworkflow-workflow-shortcut",
    );
    expect(
      screen.queryByRole("option", { name: /Parent Workflow/ }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByText(
        "Alias prod resolves to active deployment DEP-prod-1.",
      ),
    ).toBeInTheDocument();
    const contract = await screen.findByTestId(
      "inspector-subworkflow-contract",
    );
    expect(contract).toHaveTextContent("Resolved child contract");
    expect(contract).toHaveTextContent(
      "Alias prod resolves to deployment DEP-prod-1.",
    );
    expect(contract).toHaveTextContent("v3 · published");
    expect(contract).toHaveTextContent("Event · ticket.approved");
    expect(contract).toHaveTextContent("Ready to call");
    expect(contract).toHaveTextContent("5 nodes · 1 output");
    expect(contract).toHaveTextContent(
      "Parent port input becomes the child workflow run input.",
    );
    expect(screen.getByTestId("inspector-subworkflow-open")).toHaveAttribute(
      "href",
      "/caliber/workflows/wf-child",
    );

    await userEvent.selectOptions(workflowShortcut, "wf-child-2");
    expect(onChangeNode).toHaveBeenCalledWith("subflow", {
      workflow_id: "wf-child-2",
    });

    await userEvent.click(
      screen.getByTestId("inspector-subworkflow-alias-staging"),
    );
    expect(onChangeNode).toHaveBeenCalledWith("subflow", { alias: "staging" });
  });
});

describe("NodeDetailPanel", () => {
  function detailManifest(): WorkflowManifest {
    return {
      schema_version: 1,
      workflow_id: "wf-detail",
      name: "WF Detail",
      nodes: {
        start: {
          id: "start",
          type: "start",
          trigger: {
            mode: "event",
            event_name: "object.created",
            alias: "prod",
            enabled: true,
          },
          outputs: { user_message: { type: "string" } },
        },
        agent: {
          id: "agent",
          type: "agent",
          name: "support-agent",
          model: "gpt-4o-mini",
          instructions: { type: "inline", text: "Answer customer questions." },
          tools: ["lookup_policy", "issue_refund"],
          handoffs: [
            {
              target: "approval",
              description: "Escalate sensitive cases",
              condition: "if refund > 1000",
            },
          ],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
          extra_policy: { strict: true },
        },
        approval: {
          id: "approval",
          type: "human_approval",
          inputs: { request: { type: "string" } },
          outputs: { request: { type: "string" } },
        },
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
        file: {
          id: "file",
          type: "file_input",
          path: "/tmp/input.txt",
          max_bytes: 512,
          encoding: "utf-16",
          inputs: { path: { type: "string" } },
          outputs: { text: { type: "string" } },
        },
        folder: {
          id: "folder",
          type: "folder_input",
          path: "/tmp/data",
          pattern: "*.json",
          recursive: false,
          max_files: 7,
          max_bytes_per_file: 1024,
          encoding: "ascii",
          inputs: { path: { type: "string" } },
          outputs: { files: { type: "structured" } },
        },
        wait_until: {
          id: "wait_until",
          type: "wait_until",
          wait_until: "2099-01-01T00:00:00Z",
          timezone: "UTC",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
        wait_event: {
          id: "wait_event",
          type: "wait_for_event",
          event_name: "ticket.approved",
          correlation_key: "ticket_id",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
        parallel: {
          id: "parallel",
          type: "parallel",
          inputs: { input: { type: "string" } },
          outputs: { output: { type: "string" } },
        },
        join_any: {
          id: "join_any",
          type: "join",
          mode: "any",
          inputs: { left: { type: "string" }, right: { type: "string" } },
          outputs: {
            output: { type: "string" },
            merged: { type: "structured" },
          },
          execution_policy: {
            timeout_seconds: 30,
            max_retries: 1,
            idempotent: true,
          },
        },
        for_each: {
          id: "for_each",
          type: "for_each",
          target_node_id: "agent",
          item_input_port: "items",
          max_items: 25,
          inputs: { items: { type: "structured" } },
          outputs: {
            text: { type: "string" },
            results: { type: "structured" },
          },
        },
        loop: {
          id: "loop",
          type: "loop",
          target_node_id: "agent",
          max_iterations: 4,
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
        boundary: {
          id: "boundary",
          type: "error_boundary",
          target_node_id: "agent",
          fallback_text: "fallback",
          compensate_with: "approval",
          inputs: { input: { type: "string" } },
          outputs: {
            output: { type: "string" },
            error: { type: "structured" },
          },
        },
        subflow: {
          id: "subflow",
          type: "subworkflow",
          workflow_id: "wf-other",
          alias: "prod",
          timeout_seconds: 90,
          inputs: { input: { type: "string" } },
          outputs: {
            output: { type: "string" },
            result: { type: "structured" },
          },
        },
        tool_lookup: {
          id: "tool_lookup",
          type: "tool",
          tool_name: "lookup_policy",
          inputs: {
            input: { type: "string" },
            arguments: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        mcp: {
          id: "mcp",
          type: "mcp_resource",
          server_id: "MCP-1",
          tool_name: "search_docs",
          timeout_seconds: 30,
          inputs: { input: { type: "string" } },
          outputs: { text: { type: "string" }, result: { type: "structured" } },
        },
        knowledge: {
          id: "knowledge",
          type: "knowledge_query",
          knowledge_base_id: "KB-1",
          version_ids: ["KBV-2"],
          retrieval_modes: ["dense", "age_graph"],
          top_k: 8,
          chat_model: "gpt-4.1-mini",
          graph_overrides: {
            retrieval_strength: "aggressive",
            age_traversal_hops: 2,
            age_dense_rerank_weight: 0.2,
            strict_age_retrieval: true,
          },
        },
        template: {
          id: "template",
          type: "template",
          template: '{"summary":"{{input}}"}',
          output_format: "json",
          missing_variable_mode: "preserve",
          inputs: {
            input: { type: "string" },
            variables: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        external: {
          id: "external",
          type: "external_app",
          entrypoint: "support.ticketing:handle_request",
          inputs: {
            input: { type: "string" },
            context: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        python: {
          id: "python",
          type: "python_code",
          code: 'return {"text": input.upper()}',
          timeout_seconds: 11,
          inputs: {
            input: { type: "string" },
            context: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        guard: {
          id: "guard",
          type: "guardrail",
          mode: "pre_agent",
          on_failure: "redact",
          checks: [{ pii: true }],
          inputs: { response: { type: "string" } },
          outputs: { passthrough: { type: "string" } },
        },
        router: {
          id: "router",
          type: "router",
          inputs: { decision: { type: "string" } },
          outputs: {},
          branches: [
            {
              condition: { field: "intent", op: "equals", value: "refund" },
              to: "approval",
            },
            { to: "final" },
          ],
        },
        router_empty: {
          id: "router_empty",
          type: "router",
          inputs: { decision: { type: "string" } },
          outputs: {},
          branches: [],
        },
        note: {
          id: "note",
          type: "note",
          text: "Remember to include warranty details.",
        },
        ref_agent: {
          id: "ref_agent",
          type: "agent",
          instructions: {
            type: "mlflow_prompt",
            ref: "prompts:/support-agent@prod",
          },
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e1",
          from: "start",
          to: "agent",
          map: { user_message: "input" },
        },
        {
          id: "e2",
          from: "agent",
          to: "final",
          map: { final_output: "response" },
        },
      ],
      tools: {
        lookup_policy: {
          registry_ref: "tool.lookup_policy.v1",
          version_constraint: ">=1.0,<2.0",
        },
      },
    };
  }

  it("renders fallback when node id is missing", () => {
    render(
      <NodeDetailPanel
        manifest={detailManifest()}
        nodeId="missing"
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Node not found.")).toBeInTheDocument();
  });

  it("renders rich agent details, connections, and additional properties", async () => {
    const onClose = vi.fn();
    render(
      <NodeDetailPanel
        manifest={detailManifest()}
        nodeId="agent"
        onClose={onClose}
      />,
    );
    expect(screen.getByTestId("node-detail-panel")).toBeInTheDocument();
    expect(screen.getByText("Instructions")).toBeInTheDocument();
    expect(screen.getByText("lookup_policy")).toBeInTheDocument();
    expect(screen.getByText("Handoffs (1)")).toBeInTheDocument();
    expect(screen.getByText("Ports")).toBeInTheDocument();
    expect(screen.getByText("Connections")).toBeInTheDocument();
    expect(screen.getByText("Additional properties")).toBeInTheDocument();
    expect(screen.getByText("From (1)")).toBeInTheDocument();
    expect(screen.getByText("To (1)")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("node-detail-close"));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders node-type specific sections for orchestration, MCP resource, guardrail, router, approval, and note", () => {
    const m = detailManifest();
    const { rerender } = render(
      <NodeDetailPanel manifest={m} nodeId="start" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Trigger")).toBeInTheDocument();
    expect(screen.getAllByText("object.created").length).toBeGreaterThan(0);
    expect(screen.queryByText("Additional properties")).not.toBeInTheDocument();

    rerender(<NodeDetailPanel manifest={m} nodeId="file" onClose={vi.fn()} />);
    expect(screen.getByText("File source")).toBeInTheDocument();
    expect(screen.getByText("/tmp/input.txt")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="folder" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Folder source")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="wait_until" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Wait until")).toBeInTheDocument();
    expect(screen.getAllByText("2099-01-01T00:00:00Z").length).toBeGreaterThan(
      0,
    );

    rerender(
      <NodeDetailPanel manifest={m} nodeId="wait_event" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Wait for event")).toBeInTheDocument();
    expect(screen.getAllByText("ticket.approved").length).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="parallel" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Parallel")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="join_any" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Join settings")).toBeInTheDocument();
    expect(screen.getAllByText("any").length).toBeGreaterThan(0);
    expect(screen.getByText("Execution policy")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="for_each" onClose={vi.fn()} />,
    );
    expect(screen.getByText("For each settings")).toBeInTheDocument();
    expect(screen.getAllByText("25").length).toBeGreaterThan(0);

    rerender(<NodeDetailPanel manifest={m} nodeId="loop" onClose={vi.fn()} />);
    expect(screen.getByText("Loop settings")).toBeInTheDocument();
    expect(screen.getAllByText("iteration >= 2").length).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="boundary" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Error boundary")).toBeInTheDocument();
    expect(screen.getAllByText("fallback").length).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="subflow" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Subworkflow")).toBeInTheDocument();
    expect(screen.getAllByText("wf-other").length).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="tool_lookup" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Tool")).toBeInTheDocument();
    expect(screen.getAllByText("lookup_policy").length).toBeGreaterThan(0);
    expect(screen.getByText("registered_function")).toBeInTheDocument();

    rerender(<NodeDetailPanel manifest={m} nodeId="mcp" onClose={vi.fn()} />);
    expect(screen.getByText("MCP resource")).toBeInTheDocument();
    expect(screen.getAllByText("search_docs").length).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="knowledge" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Knowledge retrieval")).toBeInTheDocument();
    expect(screen.getAllByText("KB-1").length).toBeGreaterThan(0);
    expect(screen.getByText("dense, age_graph")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="template" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Template")).toBeInTheDocument();
    expect(screen.getAllByText("preserve").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/summary/).length).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="external" onClose={vi.fn()} />,
    );
    expect(screen.getByText("External app")).toBeInTheDocument();
    expect(
      screen.getAllByText("support.ticketing:handle_request").length,
    ).toBeGreaterThan(0);

    rerender(
      <NodeDetailPanel manifest={m} nodeId="python" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Python code")).toBeInTheDocument();
    expect(screen.getByText(/input.upper/)).toBeInTheDocument();

    rerender(<NodeDetailPanel manifest={m} nodeId="guard" onClose={vi.fn()} />);
    expect(screen.getByText("Guardrail settings")).toBeInTheDocument();
    expect(screen.getByText(/1 configured/)).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="router" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Routing (2 branches)")).toBeInTheDocument();
    expect(screen.getByText("IF #1")).toBeInTheDocument();
    expect(screen.getByText("ELSE")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="router_empty" onClose={vi.fn()} />,
    );
    expect(screen.getByText("No branches configured.")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={m} nodeId="approval" onClose={vi.fn()} />,
    );
    expect(screen.getByText("Human approval")).toBeInTheDocument();
    expect(screen.getByText("Required role")).toBeInTheDocument();
    expect(screen.getAllByText("caliber.approver").length).toBeGreaterThan(0);
    expect(screen.getByText("Timeout behavior")).toBeInTheDocument();

    rerender(<NodeDetailPanel manifest={m} nodeId="note" onClose={vi.fn()} />);
    expect(
      screen.getByText("Remember to include warranty details."),
    ).toBeInTheDocument();
  });

  it("renders MLflow prompt references for agent instructions", () => {
    render(
      <NodeDetailPanel
        manifest={detailManifest()}
        nodeId="ref_agent"
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Prompt ref")).toBeInTheDocument();
    expect(screen.getByText("prompts:/support-agent@prod")).toBeInTheDocument();
  });
});

describe("ProblemsPanel", () => {
  const report: ValidationReport = {
    valid: false,
    errors: [
      {
        code: "missing_tool",
        path: "nodes.agent.tools",
        message: "Tool missing",
        severity: "error",
      },
    ],
    warnings: [
      {
        code: "orphaned_node",
        path: "nodes.lonely",
        message: "Orphan",
        severity: "warning",
      },
    ],
  };

  it("renders errors + warnings and focuses node on click", async () => {
    const onFocus = vi.fn();
    render(<ProblemsPanel report={report} onFocusNode={onFocus} />);
    expect(screen.getByTestId("wf-problems-count")).toHaveTextContent(
      "1 error",
    );
    await userEvent.click(screen.getByTestId("problem-missing_tool"));
    expect(onFocus).toHaveBeenCalledWith("agent");
  });

  it("surfaces field-level focus targets when a node field is referenced", async () => {
    const onFocusIssue = vi.fn();
    render(<ProblemsPanel report={report} onFocusIssue={onFocusIssue} />);

    await userEvent.click(screen.getByTestId("problem-missing_tool"));

    expect(onFocusIssue).toHaveBeenCalledWith({
      nodeId: "agent",
      fieldKey: "tools",
      code: "missing_tool",
      path: "nodes.agent.tools",
    });
  });

  it("shows a hint when no report", () => {
    render(<ProblemsPanel report={null} />);
    expect(screen.getByText(/Run Validate/)).toBeInTheDocument();
  });
});

describe("GraphDiff", () => {
  it("renders added/removed/modified entries", () => {
    const diff: GraphDiffData = {
      added_nodes: [{ id: "guard", type: "guardrail" }],
      removed_nodes: [],
      modified_nodes: [
        { id: "agent", changes: [{ field: "tools", from: [], to: ["x"] }] },
      ],
      added_edges: ["e_new"],
      removed_edges: ["e_old"],
      modified_edges: [],
      artifact_changes: [{ kind: "prompt", ref: "p" }],
      deploy_gate_changes: [],
      empty: false,
    };
    render(<GraphDiff diff={diff} />);
    expect(screen.getByTestId("diff-added-node")).toHaveTextContent("guard");
    expect(screen.getByTestId("diff-modified-node")).toHaveTextContent("tools");
    expect(screen.getByTestId("diff-added-edge")).toHaveTextContent("e_new");
    expect(screen.getByTestId("diff-removed-edge")).toHaveTextContent("e_old");
  });

  it("shows empty state", () => {
    const diff: GraphDiffData = {
      added_nodes: [],
      removed_nodes: [],
      modified_nodes: [],
      added_edges: [],
      removed_edges: [],
      modified_edges: [],
      artifact_changes: [],
      deploy_gate_changes: [],
      empty: true,
    };
    render(<GraphDiff diff={diff} />);
    expect(screen.getByText("No graph changes.")).toBeInTheDocument();
  });
});

describe("ConnectMapPopover", () => {
  const source: ManifestNode = {
    id: "agent",
    type: "agent",
    name: "support-agent",
    outputs: {
      final_output: { type: "string" },
      tool_calls: { type: "structured" },
    },
  };
  const target: ManifestNode = {
    id: "final",
    type: "output",
    inputs: { response: { type: "string" } },
  };

  it("renders target inputs and lets operators unmap an input", async () => {
    let map: Record<string, string> = { final_output: "response" };
    const onChange = vi.fn((m: Record<string, string>) => {
      map = m;
    });
    render(
      <ConnectMapPopover
        source={source}
        target={target}
        map={map}
        onChange={onChange}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByTestId("connect-map-popover")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTestId("map-input-response"), "");
    expect(onChange).toHaveBeenCalledWith({});
  });

  it("surfaces incompatible output options without auto-selecting them", () => {
    render(
      <ConnectMapPopover
        source={source}
        target={target}
        map={{}}
        onChange={vi.fn()}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    const incompatibleOption = screen.getByRole("option", {
      name: /tool_calls .* incompatible/i,
    }) as HTMLOptionElement;
    expect(incompatibleOption.disabled).toBe(true);
  });

  it("Auto-Map matches by name", async () => {
    const onChange = vi.fn();
    render(
      <ConnectMapPopover
        source={source}
        target={target}
        map={{}}
        onChange={onChange}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByTestId("map-auto"));
    expect(onChange).toHaveBeenCalledWith({ final_output: "response" });
  });

  it("Auto-Map prefers compatible outputs over incompatible positional matches", async () => {
    const onChange = vi.fn();
    render(
      <ConnectMapPopover
        source={{
          ...source,
          outputs: {
            tool_calls: { type: "structured" },
            final_output: { type: "string" },
          },
        }}
        target={target}
        map={{}}
        onChange={onChange}
        onDone={vi.fn()}
        onRemove={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByTestId("map-auto"));
    expect(onChange).toHaveBeenCalledWith({ final_output: "response" });
  });

  it("Remove and Done fire their callbacks", async () => {
    const onDone = vi.fn();
    const onRemove = vi.fn();
    render(
      <ConnectMapPopover
        source={source}
        target={target}
        map={{ final_output: "response" }}
        onChange={vi.fn()}
        onDone={onDone}
        onRemove={onRemove}
      />,
    );
    await userEvent.click(screen.getByTestId("map-remove"));
    await userEvent.click(screen.getByTestId("map-done"));
    expect(onRemove).toHaveBeenCalled();
    expect(onDone).toHaveBeenCalled();
  });
});

describe("RouterConditionBuilder", () => {
  const nodeIds = ["billing", "final"];

  it("adds a condition", async () => {
    const onChange = vi.fn();
    render(
      <RouterConditionBuilder
        branches={[]}
        nodeIds={nodeIds}
        onChange={onChange}
      />,
    );
    await userEvent.click(screen.getByTestId("router-add-condition"));
    expect(onChange).toHaveBeenCalled();
    const branches = onChange.mock.calls[0]![0] as RouterBranch[];
    expect(branches[0]!.condition).toBeTruthy();
    expect(branches[0]!.to).toBe("billing");
  });

  it("edits field/op/value and target", async () => {
    const branches: RouterBranch[] = [
      {
        condition: { field: "output", op: "contains", value: "" },
        to: "billing",
      },
    ];
    const onChange = vi.fn();
    render(
      <RouterConditionBuilder
        branches={branches}
        nodeIds={nodeIds}
        onChange={onChange}
      />,
    );
    await userEvent.selectOptions(
      screen.getByTestId("router-op-0"),
      "mentions",
    );
    const updated = onChange.mock.calls.at(-1)![0] as RouterBranch[];
    expect(updated[0]!.condition?.op).toBe("mentions");
    await userEvent.selectOptions(screen.getByTestId("router-to-0"), "final");
    expect((onChange.mock.calls.at(-1)![0] as RouterBranch[])[0]!.to).toBe(
      "final",
    );
  });

  it("sets an else fallback branch", async () => {
    const onChange = vi.fn();
    render(
      <RouterConditionBuilder
        branches={[]}
        nodeIds={nodeIds}
        onChange={onChange}
      />,
    );
    await userEvent.selectOptions(screen.getByTestId("router-else"), "final");
    const result = onChange.mock.calls.at(-1)![0] as RouterBranch[];
    expect(result.some((b) => b.condition == null && b.to === "final")).toBe(
      true,
    );
  });
});

describe("PublishDrawer", () => {
  const cleanReport: ValidationReport = {
    valid: true,
    errors: [],
    warnings: [],
  };
  const errReport: ValidationReport = {
    valid: false,
    errors: [{ code: "x", path: "", message: "bad", severity: "error" }],
    warnings: [],
  };
  const warnReport: ValidationReport = {
    valid: true,
    errors: [],
    warnings: [
      { code: "w", path: "", message: "careful", severity: "warning" },
    ],
  };

  function setup(report: ValidationReport, onPublish = vi.fn()) {
    render(
      <PublishDrawer
        versionLabel="v3"
        report={report}
        changeSummary={["+ guardrail"]}
        publishing={false}
        onValidate={vi.fn()}
        onPublish={onPublish}
        onClose={vi.fn()}
      />,
    );
  }

  it("blocks Next when there are errors", () => {
    setup(errReport);
    expect(screen.getByTestId("publish-next-1")).toBeDisabled();
  });

  it("requires acknowledging warnings before Next", async () => {
    setup(warnReport);
    expect(screen.getByTestId("publish-next-1")).toBeDisabled();
    await userEvent.click(screen.getByTestId("publish-ack"));
    expect(screen.getByTestId("publish-next-1")).toBeEnabled();
  });

  it("walks to publish and fires onPublish", async () => {
    const onPublish = vi.fn();
    setup(cleanReport, onPublish);
    await userEvent.click(screen.getByTestId("publish-next-1"));
    await userEvent.click(screen.getByTestId("publish-next-2"));
    await userEvent.click(screen.getByTestId("publish-confirm"));
    expect(onPublish).toHaveBeenCalled();
  });
});

describe("TraceReplayGraph", () => {
  const run: WorkflowRun = {
    workflow_run_id: "WR-1",
    workflow_id: "WF-1",
    project_id: null,
    tenant_id: null,
    workflow_version_id: "WFV-1",
    deployment_alias: "prod",
    mlflow_run_id: null,
    trace_id: "trace-1",
    session_id: null,
    status: "completed",
    source: "manual",
    priority: 0,
    queued_at: "x",
    started_at: "x",
    completed_at: "y",
    summary: {
      node_path: ["start", "support_agent", "final"],
      tags: { "caliber.prompt_version": "12" },
    },
  };
  const manifest = {
    schema_version: 1,
    workflow_id: "WF-1",
    name: "WF",
    nodes: {
      start: {
        id: "start",
        type: "start" as const,
        outputs: { m: { type: "string" as const } },
      },
      support_agent: {
        id: "support_agent",
        type: "agent" as const,
        name: "support-agent",
        inputs: { input: { type: "string" as const } },
        outputs: { final_output: { type: "string" as const } },
      },
      final: {
        id: "final",
        type: "output" as const,
        inputs: { response: { type: "string" as const } },
      },
    },
    edges: [],
  };

  it("renders the path and fires Create Verification Item", async () => {
    const onCreate = vi.fn();
    const onSelectNodeId = vi.fn();
    render(
      <TraceReplayGraph
        manifest={manifest}
        run={run}
        selectedNodeId="support_agent"
        onSelectNodeId={onSelectNodeId}
        onCreateVerification={onCreate}
      />,
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "support_agent",
    );
    expect(screen.getByText(/prompt version: 12/)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("trace-path-step-0"));
    expect(onSelectNodeId).toHaveBeenCalledWith("start");
    await userEvent.click(screen.getByTestId("trace-create-verification"));
    expect(onCreate).toHaveBeenCalledWith(run);
  });

  it("surfaces AGE-backed knowledge-query diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const graphRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-AGE",
      summary: {
        node_path: ["start", "knowledge", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "question captured",
            tool_calls: [],
            handoff_target: null,
            detail: "captured graph retrieval request",
            duration_ms: 25,
          },
          {
            node_id: "knowledge",
            node_type: "knowledge_query",
            status: "ok",
            output: "Graph-backed answer",
            tool_calls: [],
            handoff_target: null,
            detail:
              "answered via age_graph · 1 citation · 1 chunk · seeded from question text",
            duration_ms: 178,
            output_by_port: {
              result: {
                versions: [
                  {
                    retrieval_mode: "age_graph",
                    citations: [
                      {
                        chunk_id: "chunk-1",
                        label: "[1] Handbook.md",
                      },
                    ],
                    retrieved_chunks: [
                      {
                        chunk_id: "chunk-1",
                        source_name: "Handbook.md",
                        source_key: "docs/handbook.md",
                        score: 0.92,
                        content: "Escalation playbook",
                        matched_entity_labels: ["Escalation Playbook"],
                      },
                    ],
                    graph_context: {
                      matched_entities: ["Escalation Playbook"],
                      age_graph_name: "knowledge_graph",
                      age_seed_strategy: "query_text",
                      strict_age_retrieval: true,
                    },
                  },
                ],
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "done",
            tool_calls: [],
            handoff_target: null,
            detail: "returned graph-backed answer",
            duration_ms: 12,
          },
        ],
      },
    };
    const graphManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        knowledge: {
          id: "knowledge",
          type: "knowledge_query" as const,
          knowledge_base_id: "KB-1",
          retrieval_modes: ["age_graph"] as const,
          inputs: { question: { type: "string" as const } },
          outputs: { answer: { type: "string" as const } },
        },
        final: manifest.nodes.final,
      },
    };

    render(
      <TraceReplayGraph
        manifest={graphManifest}
        run={graphRun}
        selectedNodeId="knowledge"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "Apache AGE graph",
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "AGE knowledge_graph",
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("1 citation");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("1 chunk");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "Seeded from question text",
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("strict AGE");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("178 ms");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "Matched Escalation Playbook",
    );
    expect(
      screen.queryByTestId("trace-create-verification"),
    ).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("trace-path-step-1"));
    expect(onSelectNodeId).toHaveBeenCalledWith("knowledge");
  });

  it("surfaces knowledge-build diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const buildRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-build",
      summary: {
        node_path: ["start", "knowledge_build", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "docs/support/",
            tool_calls: [],
            handoff_target: null,
            detail: "captured source prefix",
            duration_ms: 20,
          },
          {
            node_id: "knowledge_build",
            node_type: "knowledge_build",
            status: "ok",
            output: "Knowledge base build completed.",
            tool_calls: [],
            handoff_target: null,
            detail: "built semantic chunks and activated version",
            duration_ms: 241,
            output_by_port: {
              result: {
                status: "completed",
                knowledge_base: {
                  knowledge_base_id: "KB-1",
                  active_version_id: "KBV-3",
                },
                version: {
                  knowledge_base_version_id: "KBV-3",
                  version_number: 3,
                  status: "completed",
                  chunking_strategy: "semantic",
                  embedding_model: "intfloat/e5-large-v2",
                  graph_config: {
                    extractor_backend: "spacy",
                    output_target: "object_store_and_age",
                    default_retrieval_mode: "age_graph",
                    retrieval_strength: "balanced",
                  },
                  summary: {
                    age_sync_status: "synced",
                  },
                },
                run: {
                  knowledge_base_run_id: "KBR-3",
                  status: "completed",
                },
                await_completion: {
                  requested: true,
                  status: "completed",
                  timeout_seconds: 900,
                },
                activation: {
                  requested: true,
                  status: "activated",
                  active_version_id: "KBV-3",
                },
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "done",
            tool_calls: [],
            handoff_target: null,
            detail: "returned build result",
            duration_ms: 11,
          },
        ],
      },
    };
    const buildManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        knowledge_build: {
          id: "knowledge_build",
          type: "knowledge_build" as const,
          knowledge_base_id: "KB-1",
          inputs: { source: { type: "string" as const } },
          outputs: { result: { type: "structured" as const } },
        },
        final: manifest.nodes.final,
      },
    };

    render(
      <TraceReplayGraph
        manifest={buildManifest}
        run={buildRun}
        selectedNodeId="knowledge_build"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("Build completed")).toBeInTheDocument();
    expect(within(traceStep).getByText("KB KB-1")).toBeInTheDocument();
    expect(within(traceStep).getByText("KBV-3")).toBeInTheDocument();
    expect(within(traceStep).getByText("KBR-3")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Object store + AGE"),
    ).toBeInTheDocument();
    expect(within(traceStep).getByText("Apache AGE graph")).toBeInTheDocument();
    expect(within(traceStep).getByText("Activated KBV-3")).toBeInTheDocument();
    expect(within(traceStep).getByText("AGE synced")).toBeInTheDocument();
    expect(traceStep).toHaveTextContent("semantic");
    expect(traceStep).toHaveTextContent("intfloat/e5-large-v2");
    expect(traceStep).toHaveTextContent("spacy");
    expect(traceStep).toHaveTextContent("balanced");
    expect(traceStep).toHaveTextContent("900s timeout");
    expect(traceStep).toHaveTextContent("241 ms");

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("knowledge_build");
  });

  it("surfaces bounded loop diagnostics with polished node-type labels inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const orchestrationRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-loop",
      summary: {
        node_path: ["start", "loop", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "chunk-a.md\nchunk-b.md\nchunk-c.md",
            tool_calls: [],
            handoff_target: null,
            detail: "captured chunk set",
            duration_ms: 18,
            output_by_port: {
              items: ["chunk-a.md", "chunk-b.md", "chunk-c.md"],
            },
          },
          {
            node_id: "loop",
            node_type: "loop",
            status: "ok",
            output: "chunk-a summary\nchunk-c summary",
            tool_calls: [],
            handoff_target: null,
            detail: "processed 3 item(s) via agent (1 failed)",
            duration_ms: 312,
            output_by_port: {
              results: [
                {
                  item: "chunk-a.md",
                  output: "chunk-a summary",
                  status: "ok",
                },
                {
                  item: "chunk-b.md",
                  output: "",
                  error: "rate limit exceeded",
                },
                {
                  item: "chunk-c.md",
                  output: "chunk-c summary",
                  status: "ok",
                  artifacts: ["summary.json"],
                },
              ],
              metadata: {
                count: 3,
                failed: 1,
                target_node_id: "summarize_agent",
                target_node_type: "agent",
                artifacts: {
                  "item-0/summary.json": "{}",
                  "item-2/summary.json": "{}",
                },
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "done",
            tool_calls: [],
            handoff_target: null,
            detail: "returned loop result",
            duration_ms: 11,
          },
        ],
      },
    };
    const orchestrationManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        loop: {
          id: "loop",
          type: "loop" as const,
          target_node_id: "summarize_agent",
          max_iterations: 3,
          inputs: { input: { type: "string" as const } },
          outputs: {
            output: { type: "string" as const },
            results: { type: "structured" as const },
          },
        },
        summarize_agent: {
          id: "summarize_agent",
          type: "agent" as const,
          name: "summarize-agent",
          inputs: { input: { type: "string" as const } },
          outputs: { final_output: { type: "string" as const } },
        },
        final: manifest.nodes.final,
      },
    };

    render(
      <TraceReplayGraph
        manifest={orchestrationManifest}
        run={orchestrationRun}
        selectedNodeId="loop"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("Loop")).toBeInTheDocument();
    expect(within(traceStep).getByText("3 items")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Target summarize_agent · Agent"),
    ).toBeInTheDocument();
    expect(within(traceStep).getByText("1 failed")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Failure chunk-b.md: rate limit exceeded"),
    ).toBeInTheDocument();

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("loop");
  });

  it("surfaces child workflow diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const subworkflowRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-sub",
      summary: {
        node_path: ["start", "child_workflow", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "Escalate the refund exception.",
            tool_calls: [],
            handoff_target: null,
            detail: "captured escalation request",
            duration_ms: 20,
          },
          {
            node_id: "child_workflow",
            node_type: "subworkflow",
            status: "ok",
            output: "Escalated to the governed child workflow.",
            tool_calls: [],
            handoff_target: null,
            detail: "executed governed child workflow",
            duration_ms: 212,
            output_by_port: {
              output: "Escalated to the governed child workflow.",
              result: {
                status: "completed",
                workflow_id: "WF-child",
                alias: "prod",
                workflow_version_id: "WFV-child",
                workflow_version_number: 4,
                tokens: 17,
                steps: ["child_start", "child_review", "child_final"],
                output: "Escalated to the governed child workflow.",
              },
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "done",
            tool_calls: [],
            handoff_target: null,
            detail: "returned child workflow answer",
            duration_ms: 11,
          },
        ],
      },
    };
    const subworkflowManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        child_workflow: {
          id: "child_workflow",
          type: "subworkflow" as const,
          workflow_id: "WF-child",
          alias: "prod",
          inputs: { input: { type: "string" as const } },
          outputs: {
            output: { type: "string" as const },
            result: { type: "structured" as const },
          },
        },
        final: manifest.nodes.final,
      },
    };

    render(
      <TraceReplayGraph
        manifest={subworkflowManifest}
        run={subworkflowRun}
        selectedNodeId="child_workflow"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    expect(screen.getByTestId("trace-steps")).toHaveTextContent("WF-child");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("Alias prod");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "Child completed",
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "3 child steps",
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("17 tokens");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent(
      "Path child_start -> child_review -> child_final",
    );
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("WFV-child");
    expect(screen.getByTestId("trace-steps")).toHaveTextContent("212 ms");

    await userEvent.click(screen.getByTestId("trace-path-step-1"));
    expect(onSelectNodeId).toHaveBeenCalledWith("child_workflow");
  });

  it("surfaces direct tool-node diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const toolRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-tool",
      summary: {
        node_path: ["start", "policy_lookup", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "refund policy",
            tool_calls: [],
            handoff_target: null,
            detail: "captured request",
            duration_ms: 21,
          },
          {
            node_id: "policy_lookup",
            node_type: "tool",
            status: "ok",
            output: "Found refund policy coverage.",
            tool_calls: [],
            handoff_target: null,
            detail: "invoked lookup_policy",
            duration_ms: 153,
            output_by_port: {
              text: "Found refund policy coverage.",
              metadata: {
                tool_name: "lookup_policy",
                registry_ref: "tool:lookup_policy",
                binding_type: "registered_function",
                requires_approval: true,
                side_effect_level: "read",
                module_path: "caliber.workflows.demo_tools",
                callable_name: "lookup_policy",
                arguments: {
                  policy_id: "refund-policy",
                  topic: "refunds",
                },
              },
              tool_calls: [
                {
                  tool: "lookup_policy",
                  registry_ref: "tool:lookup_policy",
                  binding_type: "registered_function",
                  arguments: {
                    policy_id: "refund-policy",
                    topic: "refunds",
                  },
                },
              ],
            },
          },
          {
            node_id: "final",
            node_type: "output",
            status: "ok",
            output: "done",
            tool_calls: [],
            handoff_target: null,
            detail: "returned tool result",
            duration_ms: 11,
          },
        ],
      },
    };
    const toolManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        policy_lookup: {
          id: "policy_lookup",
          type: "tool" as const,
          tool_ref: "tool:lookup_policy",
          inputs: { input: { type: "string" as const } },
          outputs: {
            text: { type: "string" as const },
            result: { type: "structured" as const },
          },
        },
        final: manifest.nodes.final,
      },
    };

    render(
      <TraceReplayGraph
        manifest={toolManifest}
        run={toolRun}
        selectedNodeId="policy_lookup"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("lookup_policy")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Registered function"),
    ).toBeInTheDocument();
    expect(
      within(traceStep).getByText("tool:lookup_policy"),
    ).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Approval required"),
    ).toBeInTheDocument();
    expect(
      within(traceStep).getByText(
        "Binding caliber.workflows.demo_tools:lookup_policy",
      ),
    ).toBeInTheDocument();
    expect(
      within(traceStep).getByText("2 keys: policy_id, topic"),
    ).toBeInTheDocument();
    expect(within(traceStep).getByText("Effect read")).toBeInTheDocument();
    expect(within(traceStep).getByText("153 ms")).toBeInTheDocument();

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("policy_lookup");
  });

  it("surfaces checkpoint, pause, and resume diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const recoveryRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-recovery",
      status: "waiting_event",
      current_node_id: "wait_gate",
      summary: {
        node_path: ["start", "wait_gate"],
        resume_checkpoint_id: "CHK-2",
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "ticket request",
            tool_calls: [],
            handoff_target: null,
            detail: "captured ticket request",
            duration_ms: 18,
          },
          {
            node_id: "wait_gate",
            node_type: "wait_for_event",
            status: "ok",
            output: "waiting for ticket.approved",
            tool_calls: [],
            handoff_target: null,
            detail: "paused at event gate",
            duration_ms: 9,
          },
        ],
      },
    };
    const recoveryManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event" as const,
          inputs: { input: { type: "string" as const } },
          outputs: { output: { type: "string" as const } },
        },
      },
    };
    const recoveryEvents = [
      {
        event_id: 1,
        workflow_run_id: "WR-recovery",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "ticket request",
            tool_calls: [],
            handoff_target: null,
            detail: "captured ticket request",
            duration_ms: 18,
          },
        },
        created_at: "2026-06-13T00:00:00Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-recovery",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "wait_gate",
        payload: {
          step: {
            node_id: "wait_gate",
            node_type: "wait_for_event",
            status: "ok",
            output: "waiting for ticket.approved",
            tool_calls: [],
            handoff_target: null,
            detail: "paused at event gate",
            duration_ms: 9,
          },
        },
        created_at: "2026-06-13T00:00:02Z",
      },
      {
        event_id: 3,
        workflow_run_id: "WR-recovery",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.waiting_event",
        node_id: "wait_gate",
        payload: { node_id: "wait_gate" },
        created_at: "2026-06-13T00:00:04Z",
      },
      {
        event_id: 4,
        workflow_run_id: "WR-recovery",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.resumed",
        node_id: null,
        payload: { actor: "@ops", event_name: "ticket.approved" },
        created_at: "2026-06-13T00:00:08Z",
      },
    ];
    const recoveryCheckpoints = [
      {
        checkpoint_id: "CHK-2",
        workflow_run_id: "WR-recovery",
        project_id: null,
        sequence: 2,
        node_id: "wait_gate",
        state_blob: {
          kind: "wait_for_event",
          expected_event_name: "ticket.approved",
          output: "waiting for ticket.approved",
        },
        created_at: "2026-06-13T00:00:03Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={recoveryManifest}
        run={recoveryRun}
        events={recoveryEvents}
        checkpoints={recoveryCheckpoints}
        selectedNodeId="wait_gate"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("Event wait")).toBeInTheDocument();
    expect(within(traceStep).getByText("Resume target")).toBeInTheDocument();
    expect(within(traceStep).getByText("Paused for event")).toBeInTheDocument();
    expect(within(traceStep).getByText("Resumed")).toBeInTheDocument();
    expect(within(traceStep).getByText(/Checkpoint #2/)).toBeInTheDocument();
    expect(
      within(traceStep).getByText(/Waiting for ticket\.approved/),
    ).toBeInTheDocument();
    expect(
      within(traceStep).getByText(/Resume event ticket\.approved/),
    ).toBeInTheDocument();

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("wait_gate");
  });

  it("labels runtime approval checkpoints distinctly inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const runtimeApprovalRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-runtime-approval",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      completed_at: null,
      summary: {
        node_path: ["start", "tool_gate"],
        resume_checkpoint_id: "CHK-3",
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "delete ticket T-300",
            tool_calls: [],
            handoff_target: null,
            detail: "captured delete request",
            duration_ms: 14,
          },
          {
            node_id: "tool_gate",
            node_type: "tool",
            status: "blocked",
            output: "delete ticket T-300",
            tool_calls: [],
            handoff_target: null,
            detail: "tool execution paused for approval",
            duration_ms: 11,
          },
        ],
      },
    };
    const runtimeApprovalManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        tool_gate: {
          id: "tool_gate",
          type: "tool" as const,
          inputs: { input: { type: "string" as const } },
          outputs: {
            text: { type: "string" as const },
            result: { type: "structured" as const },
            metadata: { type: "structured" as const },
          },
        } as WorkflowManifest["nodes"][string],
      },
    };
    const runtimeApprovalEvents = [
      {
        event_id: 1,
        workflow_run_id: "WR-runtime-approval",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: runtimeApprovalRun.summary?.steps?.[0] as Record<
            string,
            unknown
          >,
        },
        created_at: "2026-06-13T00:10:00Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-runtime-approval",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "tool_gate",
        payload: {
          step: runtimeApprovalRun.summary?.steps?.[1] as Record<
            string,
            unknown
          >,
        },
        created_at: "2026-06-13T00:10:01Z",
      },
      {
        event_id: 3,
        workflow_run_id: "WR-runtime-approval",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.waiting_approval",
        node_id: "tool_gate",
        payload: { node_id: "tool_gate" },
        created_at: "2026-06-13T00:10:02Z",
      },
      {
        event_id: 4,
        workflow_run_id: "WR-runtime-approval",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.approval.approved",
        node_id: "tool_gate",
        payload: {
          runtime_approval_id: "RA-TOOL-1",
          reason: "policy reviewed",
        },
        created_at: "2026-06-13T00:10:03Z",
      },
    ];
    const runtimeApprovalCheckpoints = [
      {
        checkpoint_id: "CHK-3",
        workflow_run_id: "WR-runtime-approval",
        project_id: null,
        sequence: 3,
        node_id: "tool_gate",
        state_blob: {
          kind: "runtime_approval",
          output: "delete ticket T-300",
        },
        created_at: "2026-06-13T00:10:02Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={runtimeApprovalManifest}
        run={runtimeApprovalRun}
        events={runtimeApprovalEvents}
        checkpoints={runtimeApprovalCheckpoints}
        selectedNodeId="tool_gate"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("Runtime approval")).toBeInTheDocument();
    expect(within(traceStep).getByText("Resume target")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Paused for runtime approval"),
    ).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Runtime approval recorded"),
    ).toBeInTheDocument();
    expect(traceStep).toHaveTextContent(
      "Runtime approval RA-TOOL-1 approved: policy reviewed",
    );

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("tool_gate");
  });

  it("keeps human approval replay markers generic", async () => {
    const onSelectNodeId = vi.fn();
    const humanApprovalRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-human-approval",
      status: "waiting_approval",
      current_node_id: "review_gate",
      completed_at: null,
      summary: {
        node_path: ["start", "review_gate"],
        resume_checkpoint_id: "CHK-4",
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "approve refund R-42",
            tool_calls: [],
            handoff_target: null,
            detail: "captured approval request",
            duration_ms: 14,
          },
          {
            node_id: "review_gate",
            node_type: "human_approval",
            status: "blocked",
            output: "approve refund R-42",
            tool_calls: [],
            handoff_target: null,
            detail: "waiting on approver",
            duration_ms: 9,
          },
        ],
      },
    };
    const humanApprovalManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        review_gate: {
          id: "review_gate",
          type: "human_approval" as const,
          inputs: { input: { type: "string" as const } },
          outputs: { output: { type: "string" as const } },
        } as WorkflowManifest["nodes"][string],
      },
    };
    const humanApprovalEvents = [
      {
        event_id: 1,
        workflow_run_id: "WR-human-approval",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: humanApprovalRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:11:00Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-human-approval",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "review_gate",
        payload: {
          step: humanApprovalRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:11:01Z",
      },
      {
        event_id: 3,
        workflow_run_id: "WR-human-approval",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.waiting_approval",
        node_id: "review_gate",
        payload: { node_id: "review_gate" },
        created_at: "2026-06-13T00:11:02Z",
      },
    ];
    const humanApprovalCheckpoints = [
      {
        checkpoint_id: "CHK-4",
        workflow_run_id: "WR-human-approval",
        project_id: null,
        sequence: 3,
        node_id: "review_gate",
        state_blob: {
          kind: "human_approval",
          output: "approve refund R-42",
        },
        created_at: "2026-06-13T00:11:02Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={humanApprovalManifest}
        run={humanApprovalRun}
        events={humanApprovalEvents}
        checkpoints={humanApprovalCheckpoints}
        selectedNodeId="review_gate"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("Approval gate")).toBeInTheDocument();
    expect(within(traceStep).getByText("Resume target")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Paused for approval"),
    ).toBeInTheDocument();
    expect(
      within(traceStep).queryByText("Paused for runtime approval"),
    ).not.toBeInTheDocument();

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("review_gate");
  });

  it("shows inherited checkpoint provenance inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const inheritedApprovalRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-inherited-approval",
      status: "waiting_approval",
      current_node_id: "review_gate",
      completed_at: null,
      summary: {
        retry_of: "WR-parent",
        retry_mode: "checkpoint",
        resume_checkpoint_id: "CHK-parent",
        resume_checkpoint_run_id: "WR-parent",
        node_path: ["start", "review_gate"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "approve refund R-42",
            tool_calls: [],
            handoff_target: null,
            detail: "captured approval request",
            duration_ms: 14,
          },
          {
            node_id: "review_gate",
            node_type: "human_approval",
            status: "blocked",
            output: "approve refund R-42",
            tool_calls: [],
            handoff_target: null,
            detail: "waiting on approver",
            duration_ms: 9,
          },
        ],
      },
    };
    const inheritedApprovalManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        review_gate: {
          id: "review_gate",
          type: "human_approval" as const,
          inputs: { input: { type: "string" as const } },
          outputs: { output: { type: "string" as const } },
        } as WorkflowManifest["nodes"][string],
      },
    };
    const inheritedApprovalEvents = [
      {
        event_id: 1,
        workflow_run_id: "WR-inherited-approval",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: inheritedApprovalRun.summary?.steps?.[0] as Record<
            string,
            unknown
          >,
        },
        created_at: "2026-06-13T00:12:00Z",
      },
      {
        event_id: 2,
        workflow_run_id: "WR-inherited-approval",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "review_gate",
        payload: {
          step: inheritedApprovalRun.summary?.steps?.[1] as Record<
            string,
            unknown
          >,
        },
        created_at: "2026-06-13T00:12:01Z",
      },
      {
        event_id: 3,
        workflow_run_id: "WR-inherited-approval",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.waiting_approval",
        node_id: "review_gate",
        payload: { node_id: "review_gate" },
        created_at: "2026-06-13T00:12:02Z",
      },
    ];
    const inheritedApprovalCheckpoints = [
      {
        checkpoint_id: "CHK-parent",
        workflow_run_id: "WR-parent",
        project_id: null,
        sequence: 7,
        node_id: "review_gate",
        state_blob: {
          kind: "human_approval",
          output: "approve refund R-42",
        },
        created_at: "2026-06-13T00:11:02Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={inheritedApprovalManifest}
        run={inheritedApprovalRun}
        events={inheritedApprovalEvents}
        checkpoints={inheritedApprovalCheckpoints}
        selectedNodeId="review_gate"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const traceStep = screen.getByTestId("trace-path-step-1");
    expect(within(traceStep).getByText("Approval gate")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Inherited checkpoint"),
    ).toBeInTheDocument();
    expect(within(traceStep).getByText("Resume target")).toBeInTheDocument();
    expect(
      within(traceStep).getByText("Paused for approval"),
    ).toBeInTheDocument();
    expect(traceStep).toHaveTextContent("Checkpoint #7 from WR-parent");

    await userEvent.click(traceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("review_gate");
  });

  it("surfaces queued, started, cancel-requested, and cancelled diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const cancelledRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-cancelled",
      status: "cancelled",
      current_node_id: null,
      cancel_requested_at: "2026-06-13T00:30:04Z",
      cancel_reason: "operator stop",
      completed_at: "2026-06-13T00:30:05Z",
      summary: {
        node_path: ["start", "wait_gate"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "ticket status",
            tool_calls: [],
            handoff_target: null,
            detail: "captured status request",
            duration_ms: 14,
          },
          {
            node_id: "wait_gate",
            node_type: "wait_for_event",
            status: "ok",
            output: "waiting for approval",
            tool_calls: [],
            handoff_target: null,
            detail: "paused for approval event",
            duration_ms: 7,
          },
        ],
      },
    };
    const cancelledManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event" as const,
          inputs: { input: { type: "string" as const } },
          outputs: { output: { type: "string" as const } },
        },
      },
    };
    const cancelledEvents = [
      {
        event_id: 31,
        workflow_run_id: "WR-cancelled",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.queued",
        node_id: null,
        payload: { actor: "@ops" },
        created_at: "2026-06-13T00:30:00Z",
      },
      {
        event_id: 32,
        workflow_run_id: "WR-cancelled",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { worker_id: "worker-1" },
        created_at: "2026-06-13T00:30:01Z",
      },
      {
        event_id: 33,
        workflow_run_id: "WR-cancelled",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: cancelledRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:30:01Z",
      },
      {
        event_id: 34,
        workflow_run_id: "WR-cancelled",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.step",
        node_id: "wait_gate",
        payload: {
          step: cancelledRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:30:03Z",
      },
      {
        event_id: 35,
        workflow_run_id: "WR-cancelled",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.cancel_requested",
        node_id: "wait_gate",
        payload: { reason: "operator stop" },
        created_at: "2026-06-13T00:30:04Z",
      },
      {
        event_id: 36,
        workflow_run_id: "WR-cancelled",
        project_id: null,
        sequence: 6,
        event_type: "workflow.run.cancelled",
        node_id: null,
        payload: { reason: "operator stop" },
        created_at: "2026-06-13T00:30:05Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={cancelledManifest}
        run={cancelledRun}
        events={cancelledEvents}
        selectedNodeId="wait_gate"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const startTraceStep = screen.getByTestId("trace-path-step-0");
    expect(within(startTraceStep).getByText("Queued")).toBeInTheDocument();
    expect(within(startTraceStep).getByText("Run started")).toBeInTheDocument();

    const cancelledTraceStep = screen.getByTestId("trace-path-step-1");
    expect(
      within(cancelledTraceStep).getByText("Cancel requested"),
    ).toBeInTheDocument();
    expect(
      within(cancelledTraceStep).getByText("Run cancelled"),
    ).toBeInTheDocument();
    expect(cancelledTraceStep).toHaveTextContent(
      "Cancel requested: operator stop",
    );
    expect(cancelledTraceStep).toHaveTextContent("Cancelled: operator stop");

    await userEvent.click(cancelledTraceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("wait_gate");
  });

  it("surfaces recovered re-queue diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const recoveredRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-recovered",
      status: "queued",
      current_node_id: null,
      completed_at: null,
      summary: {
        node_path: ["start", "support_agent"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "Need the escalation policy",
            tool_calls: [],
            handoff_target: null,
            detail: "captured request",
            duration_ms: 12,
          },
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "ok",
            output: "Lease expired mid-turn",
            tool_calls: [],
            handoff_target: null,
            detail: "worker lost lease before checkpoint",
            duration_ms: 91,
          },
        ],
      },
    };
    const recoveredEvents = [
      {
        event_id: 61,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: recoveredRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:40:00Z",
      },
      {
        event_id: 62,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.step",
        node_id: "support_agent",
        payload: {
          step: recoveredRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:40:01Z",
      },
      {
        event_id: 63,
        workflow_run_id: "WR-recovered",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.recovered",
        node_id: null,
        payload: {
          reason: "lease_expired",
          worker_id: "worker-7",
        },
        created_at: "2026-06-13T00:40:02Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={manifest}
        run={recoveredRun}
        events={recoveredEvents}
        selectedNodeId="support_agent"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const recoveredTraceStep = screen.getByTestId("trace-path-step-1");
    expect(
      within(recoveredTraceStep).getByText("Recovered"),
    ).toBeInTheDocument();
    expect(recoveredTraceStep).toHaveTextContent(
      "Recovered by worker-7: worker lease expired",
    );

    await userEvent.click(recoveredTraceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("support_agent");
  });

  it("surfaces expired diagnostics inside the replay path", async () => {
    const onSelectNodeId = vi.fn();
    const expiredRun: WorkflowRun = {
      ...run,
      workflow_run_id: "WR-expired",
      status: "expired",
      current_node_id: null,
      completed_at: null,
      lease_expires_at: "2026-06-13T00:31:05Z",
      summary: {
        node_path: ["start", "wait_gate"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "ticket status",
            tool_calls: [],
            handoff_target: null,
            detail: "captured status request",
            duration_ms: 14,
          },
          {
            node_id: "wait_gate",
            node_type: "wait_for_event",
            status: "ok",
            output: "waiting for approval",
            tool_calls: [],
            handoff_target: null,
            detail: "paused for approval event",
            duration_ms: 7,
          },
        ],
      },
    };
    const expiredManifest = {
      ...manifest,
      nodes: {
        start: manifest.nodes.start,
        wait_gate: {
          id: "wait_gate",
          type: "wait_for_event" as const,
          inputs: { input: { type: "string" as const } },
          outputs: { output: { type: "string" as const } },
        },
      },
    };
    const expiredEvents = [
      {
        event_id: 41,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.queued",
        node_id: null,
        payload: { actor: "@ops" },
        created_at: "2026-06-13T00:31:00Z",
      },
      {
        event_id: 42,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.started",
        node_id: null,
        payload: { worker_id: "worker-9" },
        created_at: "2026-06-13T00:31:01Z",
      },
      {
        event_id: 43,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "start",
        payload: {
          step: expiredRun.summary?.steps?.[0] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:31:01Z",
      },
      {
        event_id: 44,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.step",
        node_id: "wait_gate",
        payload: {
          step: expiredRun.summary?.steps?.[1] as Record<string, unknown>,
        },
        created_at: "2026-06-13T00:31:03Z",
      },
      {
        event_id: 45,
        workflow_run_id: "WR-expired",
        project_id: null,
        sequence: 5,
        event_type: "workflow.run.expired",
        node_id: null,
        payload: { reason: "worker lease lost" },
        created_at: "2026-06-13T00:31:05Z",
      },
    ];

    render(
      <TraceReplayGraph
        manifest={expiredManifest}
        run={expiredRun}
        events={expiredEvents}
        selectedNodeId="wait_gate"
        onSelectNodeId={onSelectNodeId}
      />,
    );

    const expiredTraceStep = screen.getByTestId("trace-path-step-1");
    expect(
      within(expiredTraceStep).getByText("Run expired"),
    ).toBeInTheDocument();
    expect(expiredTraceStep).toHaveTextContent("Expired: worker lease lost");

    await userEvent.click(expiredTraceStep);
    expect(onSelectNodeId).toHaveBeenCalledWith("wait_gate");
  });
});

describe("Component enhancements (label / advanced / output / legacy)", () => {
  function apiManifest(): WorkflowManifest {
    return {
      schema_version: 1,
      workflow_id: "wf",
      name: "WF",
      nodes: {
        api: {
          id: "api",
          type: "api_request",
          mode: "url",
          url: "",
          method: "GET",
          headers: {},
          timeout_seconds: 30,
          inputs: { input: { type: "string" } },
          outputs: { text: { type: "string" } },
        },
      },
      edges: [],
    } as unknown as WorkflowManifest;
  }

  // A — per-node display name + description
  it("applies reusable operational connector presets without embedding credentials", () => {
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={apiManifest()}
        selectedNodeId="api"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("inspector-api-connector-preset"), {
      target: { value: "deployment-health" },
    });

    expect(onChangeNode).toHaveBeenCalledWith("api", {
      mode: "url",
      method: "GET",
      url: "https://DEPLOYMENT_API.example.invalid/v1/deployments/DEPLOYMENT/health",
      headers: { Accept: "application/json" },
      body: "",
    });
    expect(screen.getByText(/never paste a credential/i)).toBeInTheDocument();
  });

  // A — per-node display name + description
  it("edits a node's display name and description", () => {
    const onChangeNode = vi.fn();
    render(
      <Inspector
        manifest={apiManifest()}
        selectedNodeId="api"
        tools={[]}
        onChangeNode={onChangeNode}
        onChangeWorkflow={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("inspector-node-label"), {
      target: { value: "Fetch order" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("api", { label: "Fetch order" });
    fireEvent.change(screen.getByTestId("inspector-node-description"), {
      target: { value: "Calls the orders API." },
    });
    expect(onChangeNode).toHaveBeenCalledWith("api", {
      description: "Calls the orders API.",
    });
  });

  // B — advanced field toggle
  it("hides advanced fields until Show advanced is clicked", async () => {
    const spec: WorkflowComponent = {
      type: "api_request",
      label: "API Request",
      category: "Integrations",
      description: "",
      docs: [],
      default_inputs: {},
      default_outputs: {},
      fields: [
        {
          key: "url",
          label: "URL",
          type: "string",
          required: false,
          default: "",
          advanced: false,
          constraints: {},
          examples: [],
        },
        {
          key: "timeout_seconds",
          label: "Timeout",
          type: "number",
          required: false,
          default: 30,
          advanced: true,
          constraints: {},
          examples: [],
        },
      ],
      setup_checks: [],
    };
    render(
      <Inspector
        manifest={apiManifest()}
        selectedNodeId="api"
        tools={[]}
        componentSpec={spec}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(screen.getByTestId("inspector-field-url")).toBeInTheDocument();
    expect(
      screen.queryByTestId("inspector-field-timeout_seconds"),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("inspector-toggle-advanced"));
    expect(
      screen.getByTestId("inspector-field-timeout_seconds"),
    ).toBeInTheDocument();
  });

  // C — inline last output
  it("shows the node's last output when a step is provided", () => {
    const step = {
      node_id: "api",
      node_type: "api_request",
      status: "ok",
      output: "Hello world",
      tool_calls: [],
      handoff_target: null,
      detail: "POST https://x -> 200",
      duration_ms: 12,
    };
    render(
      <Inspector
        manifest={apiManifest()}
        selectedNodeId="api"
        tools={[]}
        lastStep={step}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    const out = screen.getByTestId("inspector-node-output");
    expect(out).toHaveTextContent("Hello world");
    expect(out).toHaveTextContent("ok");
  });

  it("omits the output section when there is no step", () => {
    render(
      <Inspector
        manifest={apiManifest()}
        selectedNodeId="api"
        tools={[]}
        onChangeNode={vi.fn()}
        onChangeWorkflow={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("inspector-node-output"),
    ).not.toBeInTheDocument();
  });

  // D — legacy palette
  it("hides legacy components until Show legacy is toggled, with a badge", async () => {
    const components: WorkflowComponent[] = [
      {
        type: "external_app",
        label: "External App",
        category: "Integrations",
        description: "migration bridge",
        docs: [],
        default_inputs: {},
        default_outputs: {},
        fields: [],
        setup_checks: [],
        legacy: true,
        legacy_replacement: "Tool, Python Code, or API Request",
      },
    ];
    render(<NodePalette onAddNode={vi.fn()} components={components} />);
    // legacy item hidden by default
    expect(
      screen.queryByTestId("palette-external_app"),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("palette-show-legacy"));
    expect(screen.getByTestId("palette-external_app")).toBeInTheDocument();
    expect(
      screen.getByTestId("palette-legacy-external_app"),
    ).toBeInTheDocument();
  });
});
