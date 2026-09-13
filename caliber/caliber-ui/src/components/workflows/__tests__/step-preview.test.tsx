/**
 * Tests for the per-step preview surface (Lakeflow "what changed at every step").
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PreviewStep } from "@/api/workflowTypes";
import {
  StepPreview,
  describeStepChange,
  stepStatusStyle,
  workflowNodeTypeLabel,
  ageSeedStrategyLabel,
  extractErrorBoundaryDiagnostics,
  extractForEachDiagnostics,
  extractJoinDiagnostics,
  extractKnowledgeBuildDiagnostics,
  extractKnowledgeQueryDiagnostics,
  extractStepTelemetry,
  extractStorageNodeDiagnostics,
  extractSubworkflowDiagnostics,
  extractToolNodeDiagnostics,
  formatUsdEstimate,
  knowledgeBuildActivationStatusLabel,
  knowledgeBuildStatusLabel,
  knowledgeBuildWaitStatusLabel,
  knowledgeGraphTargetLabel,
  retrievalModeLabel,
  storageCountLabel,
  storageItemUnit,
  subworkflowStatusLabel,
  subworkflowStatusTone,
  toolArgumentSummary,
  toolBindingTargetLabel,
  toolBindingTypeLabel,
  toolSideEffectLabel,
} from "@/components/workflows/StepPreview";
import type {
  ErrorBoundaryDiagnostics,
  ForEachDiagnostics,
  JoinDiagnostics,
  KnowledgeBuildDiagnostics,
  KnowledgeQueryDiagnostics,
  StorageNodeDiagnostics,
  SubworkflowDiagnostics,
  ToolNodeDiagnostics,
} from "@/components/workflows/StepPreview";

function step(overrides: Partial<PreviewStep> = {}): PreviewStep {
  return {
    node_id: "rules",
    node_type: "agent",
    status: "ok",
    output: "extracted 12 rules",
    tool_calls: [],
    handoff_target: null,
    detail: "",
    ...overrides,
  };
}

function knowledgeQueryDiag(
  overrides: Partial<KnowledgeQueryDiagnostics> = {},
): KnowledgeQueryDiagnostics {
  return {
    retrievalMode: null,
    resultCount: 0,
    citations: [],
    chunks: [],
    matchedEntities: [],
    expandedEntities: [],
    ageGraphName: null,
    ageStatus: null,
    ageSeedStrategy: null,
    ageMatchedChunkCount: null,
    ageTraversalHops: null,
    ageCandidatePoolSize: null,
    ageDenseRerankWeight: null,
    retrievalStrength: null,
    minimumRelationshipWeight: null,
    fallbackReason: null,
    ageFallbackReason: null,
    fallbackRetrievalMode: null,
    strictAgeRetrieval: false,
    queryOverrideActive: false,
    ...overrides,
  };
}

function knowledgeBuildDiag(
  overrides: Partial<KnowledgeBuildDiagnostics> = {},
): KnowledgeBuildDiagnostics {
  return {
    knowledgeBaseId: null,
    activeVersionId: null,
    versionId: null,
    versionNumber: null,
    status: null,
    runId: null,
    runStatus: null,
    chunkingStrategy: null,
    embeddingModel: null,
    waitRequested: false,
    waitStatus: null,
    waitTimeoutSeconds: null,
    activationRequested: false,
    activationStatus: null,
    activationActiveVersionId: null,
    graphTarget: null,
    graphExtractor: null,
    defaultRetrievalMode: null,
    retrievalStrength: null,
    ageSyncStatus: null,
    previewSkipped: false,
    ...overrides,
  };
}

function forEachDiag(
  overrides: Partial<ForEachDiagnostics> = {},
): ForEachDiagnostics {
  return {
    count: 0,
    failed: 0,
    targetNodeId: null,
    targetNodeType: null,
    artifactCount: 0,
    results: [],
    ...overrides,
  };
}

function joinDiag(overrides: Partial<JoinDiagnostics> = {}): JoinDiagnostics {
  return {
    branchCount: 0,
    mergedKeys: [],
    outputPreview: null,
    ...overrides,
  };
}

function errorBoundaryDiag(
  overrides: Partial<ErrorBoundaryDiagnostics> = {},
): ErrorBoundaryDiagnostics {
  return {
    message: null,
    targetNodeId: null,
    targetNodeType: null,
    compensationNodeId: null,
    compensationNodeType: null,
    compensationOutputPreview: null,
    artifactCount: 0,
    ...overrides,
  };
}

function subworkflowDiag(
  overrides: Partial<SubworkflowDiagnostics> = {},
): SubworkflowDiagnostics {
  return {
    childStatus: null,
    workflowId: null,
    alias: null,
    workflowVersionId: null,
    workflowVersionNumber: null,
    tokens: null,
    steps: [],
    stepCount: 0,
    outputPreview: null,
    error: null,
    ...overrides,
  };
}

function toolNodeDiag(
  overrides: Partial<ToolNodeDiagnostics> = {},
): ToolNodeDiagnostics {
  return {
    localName: null,
    registryRef: null,
    bindingType: null,
    callCount: 0,
    requiresApproval: false,
    sideEffectLevel: null,
    modulePath: null,
    callableName: null,
    serverId: null,
    remoteToolName: null,
    argumentKeys: [],
    resultPreview: null,
    ...overrides,
  };
}

function storageNodeDiag(
  overrides: Partial<StorageNodeDiagnostics> = {},
): StorageNodeDiagnostics {
  return {
    nodeType: "file_input",
    direction: "input",
    path: null,
    bucket: null,
    prefix: null,
    pattern: null,
    recursive: false,
    encoding: null,
    count: null,
    matchedCount: null,
    skippedCount: null,
    truncatedList: false,
    entries: [],
    ...overrides,
  };
}

describe("describeStepChange", () => {
  it("prefers the step detail (guardrail redact / block reason)", () => {
    expect(
      describeStepChange(step({ detail: "redacted: pii_detection" })),
    ).toBe("redacted: pii_detection");
  });

  it("augments storage detail when bucket diagnostics add skips or truncation", () => {
    expect(
      describeStepChange(
        step({
          node_type: "input_bucket",
          detail: "read 1 object(s) from docs/run1/",
          output_by_port: {
            files: [
              {
                key: "docs/run1/a.txt",
                relative_path: "a.txt",
                bytes: 5,
                truncated: false,
                text: "hello",
              },
            ],
            metadata: {
              bucket: "docs",
              prefix: "run1/",
              object_count: 1,
              skipped_object_count: 1,
              truncated_file_list: true,
            },
          },
        }),
      ),
    ).toBe("read 1 object(s) from docs/run1/ (1 skipped, listing truncated)");
  });

  it("falls back to the handoff/router target", () => {
    expect(describeStepChange(step({ handoff_target: "optimize" }))).toBe(
      "→ optimize",
    );
  });

  it("falls back to tool-call count (singular/plural)", () => {
    expect(describeStepChange(step({ tool_calls: [{}, {}] }))).toBe(
      "used 2 tools",
    );
    expect(describeStepChange(step({ tool_calls: [{}] }))).toBe("used 1 tool");
  });

  it("humanizes internal wait and approval detail markers", () => {
    expect(
      describeStepChange(
        step({
          node_type: "wait_for_event",
          status: "blocked",
          detail: "waiting_event:resume_gate",
        }),
      ),
    ).toBe("waiting for a resume event");
    expect(
      describeStepChange(
        step({
          node_type: "human_approval",
          status: "blocked",
          detail: "waiting_approval:review",
        }),
      ),
    ).toBe("awaiting human approval");
  });

  it("covers the remaining wait/approval node-type branches", () => {
    expect(
      describeStepChange(
        step({ node_type: "wait_until", status: "blocked", detail: "waiting_event:t" }),
      ),
    ).toBe("paused until the scheduled resume time");
    expect(
      describeStepChange(
        step({ node_type: "wait_event", status: "blocked", detail: "waiting_event:t" }),
      ),
    ).toBe("waiting for a resume event");
    expect(
      describeStepChange(
        step({ node_type: "agent", status: "blocked", detail: "waiting_event:t" }),
      ),
    ).toBe("waiting for an external resume event");
    expect(
      describeStepChange(
        step({ node_type: "agent", status: "blocked", detail: "waiting_approval:t" }),
      ),
    ).toBe("awaiting runtime approval");
  });

  it("summarizes storage, knowledge, loop, and child-workflow steps from diagnostics", () => {
    expect(
      describeStepChange(
        step({
          node_type: "output_bucket",
          detail: "",
          output_by_port: {
            keys: ["runs/out-1.json", "runs/out-2.json"],
            metadata: {
              bucket: "artifacts",
              prefix: "runs",
            },
          },
        }),
      ),
    ).toBe("wrote 2 objects to artifacts/runs");

    expect(
      describeStepChange(
        step({
          node_type: "input_bucket",
          detail: "",
          output_by_port: {
            files: [
              {
                key: "docs/run1/a.txt",
                relative_path: "a.txt",
                bytes: 5,
                truncated: false,
                text: "hello",
              },
            ],
            metadata: {
              bucket: "docs",
              prefix: "run1/",
              object_count: 1,
              skipped_object_count: 1,
            },
          },
        }),
      ),
    ).toBe("loaded 1 object from docs/run1/ (1 skipped)");

    expect(
      describeStepChange(
        step({
          node_type: "knowledge_build",
          detail: "",
          output_by_port: {
            result: {
              status: "completed",
              knowledge_base: {
                knowledge_base_id: "KB-1",
              },
              version: {
                version_number: 3,
              },
            },
          },
        }),
      ),
    ).toBe("built knowledge base KB-1 v3");

    expect(
      describeStepChange(
        step({
          node_type: "for_each",
          detail: "",
          output_by_port: {
            results: [{ item: "a" }, { item: "b" }, { item: "c" }],
            metadata: {
              count: 3,
              failed: 1,
              target_node_id: "summarize_agent",
            },
          },
        }),
      ),
    ).toBe("processed 3 items via summarize_agent (1 failed)");

    expect(
      describeStepChange(
        step({
          node_type: "subworkflow",
          detail: "",
          output_by_port: {
            result: {
              status: "completed",
              workflow_id: "WF-child",
              alias: "prod",
            },
          },
        }),
      ),
    ).toBe("completed child workflow WF-child@prod");
  });

  it("falls back to the raw status", () => {
    expect(describeStepChange(step({ status: "blocked" }))).toBe("blocked");
  });
});

describe("stepStatusStyle", () => {
  it("maps known statuses and defaults unknown ones", () => {
    expect(stepStatusStyle("ok")).toContain("emerald");
    expect(stepStatusStyle("blocked")).toContain("red");
    expect(stepStatusStyle("skipped")).toContain("zinc");
    expect(stepStatusStyle("mystery")).toContain("zinc");
  });
});

describe("workflowNodeTypeLabel", () => {
  it("returns polished labels for runtime node types", () => {
    expect(workflowNodeTypeLabel("wait_for_event")).toBe("Wait for event");
    expect(workflowNodeTypeLabel("parallel")).toBe("Parallel");
    expect(workflowNodeTypeLabel("join")).toBe("Join");
    expect(workflowNodeTypeLabel("tool")).toBe("Tool");
    expect(workflowNodeTypeLabel("template")).toBe("Template");
    expect(workflowNodeTypeLabel("router")).toBe("Router");
    expect(workflowNodeTypeLabel("guardrail")).toBe("Guardrail");
    expect(workflowNodeTypeLabel("start")).toBe("Start");
    expect(workflowNodeTypeLabel("output")).toBe("Output");
  });

  it("returns null for a null node type and humanizes fully unmapped ones", () => {
    expect(workflowNodeTypeLabel(null)).toBeNull();
    expect(workflowNodeTypeLabel("external_app")).toBe("External app");
    expect(workflowNodeTypeLabel("note")).toBe("Note");
    expect(workflowNodeTypeLabel("mcp_resource")).toBe("MCP resource");
    expect(workflowNodeTypeLabel("python_code")).toBe("Python code");
    expect(workflowNodeTypeLabel("totally_custom_type")).toBe("totally custom type");
  });
});

describe("StepPreview", () => {
  it("renders status, output, what-changed, tool count, and duration", () => {
    render(
      <StepPreview
        step={step({
          status: "ok",
          output: "extracted 12 rules",
          detail: "redacted: pii_detection",
          tool_calls: [{}],
          duration_ms: 1234,
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-status")).toHaveTextContent("ok");
    expect(screen.getByTestId("step-preview-output")).toHaveTextContent(
      "extracted 12 rules",
    );
    expect(screen.getByTestId("step-preview-change")).toHaveTextContent(
      "redacted: pii_detection",
    );
    expect(screen.getByTestId("step-preview-tools")).toHaveTextContent(
      "1 tool call",
    );
    expect(screen.getByText(/1234 ms/)).toBeInTheDocument();
  });

  it("surfaces model, prompt version, and token telemetry when present", () => {
    render(
      <StepPreview
        step={step({
          node_id: "writer",
          node_type: "agent",
          output: "Drafted the customer reply.",
          model: "gpt-4.1-mini",
          tokens: 42,
          prompt_tokens: 18,
          completion_tokens: 24,
          cached_prompt_tokens: 12,
          cost_usd: 0.000042,
          prompt_version: "openai_responses",
        })}
      />,
    );

    const telemetry = screen.getByTestId("step-preview-telemetry");
    expect(telemetry).toHaveTextContent("LLM Telemetry");
    expect(telemetry).toHaveTextContent("gpt-4.1-mini");
    expect(telemetry).toHaveTextContent("42 tokens");
    expect(telemetry).toHaveTextContent("Prompt openai_responses");
    expect(telemetry).toHaveTextContent("18 prompt");
    expect(telemetry).toHaveTextContent("24 completion");
    expect(telemetry).toHaveTextContent("12 cached prompt");
    expect(telemetry).toHaveTextContent("Est. $0.000042");
  });

  it("shows upstream node output(s) as the step input", () => {
    render(
      <StepPreview
        step={step()}
        upstream={[{ nodeId: "entities", output: "ENTITY LIST" }]}
      />,
    );
    const input = screen.getByTestId("step-preview-input");
    expect(input).toHaveTextContent("Input (1)");
    expect(input).toHaveTextContent("entities:");
    expect(input).toHaveTextContent("ENTITY LIST");
  });

  it("renders an em-dash for empty output", () => {
    render(<StepPreview step={step({ output: "" })} />);
    expect(screen.getByTestId("step-preview-output")).toHaveTextContent("—");
  });

  it("surfaces direct tool-node diagnostics and the text-port result preview", () => {
    render(
      <StepPreview
        step={step({
          node_id: "policy_lookup",
          node_type: "tool",
          detail: "invoked lookup_policy",
          output: "",
          output_by_port: {
            text: "Found support refund policy coverage.",
            result: {
              status: "ok",
              matched_policy: "refund-support",
            },
            metadata: {
              tool_name: "lookup_policy",
              registry_ref: "tool:lookup_policy",
              binding_type: "registered_function",
              requires_approval: true,
              side_effect_level: "read",
              module_path: "caliber.workflows.demo_tools",
              callable_name: "lookup_policy",
              arguments: {
                policy_id: "refund-support",
                topic: "refunds",
              },
            },
            tool_calls: [
              {
                tool: "lookup_policy",
                registry_ref: "tool:lookup_policy",
                binding_type: "registered_function",
                arguments: {
                  policy_id: "refund-support",
                  topic: "refunds",
                },
                result: {
                  text: "Found support refund policy coverage.",
                },
              },
            ],
          },
        })}
      />,
    );

    const toolCard = screen.getByTestId("step-preview-tool-node");
    expect(toolCard).toHaveTextContent("Tool Execution");
    expect(toolCard).toHaveTextContent("lookup_policy");
    expect(toolCard).toHaveTextContent("Registered function");
    expect(toolCard).toHaveTextContent("tool:lookup_policy");
    expect(toolCard).toHaveTextContent("Approval required");
    expect(toolCard).toHaveTextContent(
      "caliber.workflows.demo_tools:lookup_policy",
    );
    expect(toolCard).toHaveTextContent("2 keys: policy_id, topic");
    expect(toolCard).toHaveTextContent("Found support refund policy coverage.");
  });

  it("surfaces knowledge-query graph diagnostics, citations, and chunk previews", () => {
    render(
      <StepPreview
        step={step({
          node_id: "knowledge",
          node_type: "knowledge_query",
          output: "Bob owns Platform reliability.",
          output_by_port: {
            answer: "Bob owns Platform reliability.",
            citations: [
              {
                chunk_id: "CH-1",
                label: "incident-playbook.md",
              },
            ],
            chunks: [
              {
                chunk_id: "CH-1",
                source_name: "incident-playbook.md",
                source_key: "docs/incident-playbook.md",
                score: 1.21,
                content: "Alice leads Support. Bob owns Platform reliability.",
                matched_entity_labels: ["Bob", "Platform reliability"],
              },
            ],
            graph_context: {
              matched_entities: ["Bob"],
              expanded_entities: ["Platform reliability"],
              age_graph_name: "knowledge_graph",
              age_status: "ok",
              age_seed_strategy: "query_text",
              age_matched_chunk_count: 7,
              age_traversal_hops: 1,
              age_candidate_pool_size: 24,
              age_dense_rerank_weight: 0.15,
              retrieval_strength: "balanced",
              minimum_relationship_weight: 2.5,
              strict_age_retrieval: true,
              query_override_active: true,
            },
            result: {
              versions: [
                {
                  retrieval_mode: "age_graph",
                },
              ],
            },
          },
        })}
      />,
    );

    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("Apache AGE graph");
    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("knowledge_graph");
    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("Seeded from question text");
    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("Matched entities");
    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("Expanded neighborhood");
    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("Graph tuning:");
    expect(
      screen.getByTestId("step-preview-knowledge-query"),
    ).toHaveTextContent("Strict AGE");
    expect(
      screen.getByTestId("step-preview-knowledge-citations"),
    ).toHaveTextContent("incident-playbook.md");
    expect(
      screen.getByTestId("step-preview-knowledge-chunks"),
    ).toHaveTextContent("Matched 7 before rerank");
    expect(
      screen.getByTestId("step-preview-knowledge-chunks"),
    ).toHaveTextContent("Bob owns Platform reliability.");
  });

  it("surfaces knowledge-build version, activation, and graph diagnostics", () => {
    render(
      <StepPreview
        step={step({
          node_id: "knowledge_build",
          node_type: "knowledge_build",
          detail: "built a governed knowledge-base version",
          output: "Knowledge base build completed.",
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
        })}
      />,
    );

    const buildCard = screen.getByTestId("step-preview-knowledge-build");
    expect(buildCard).toHaveTextContent("Knowledge Build");
    expect(buildCard).toHaveTextContent("Build completed");
    expect(buildCard).toHaveTextContent("KB KB-1");
    expect(buildCard).toHaveTextContent("KBV-3");
    expect(buildCard).toHaveTextContent("KBR-3");
    expect(buildCard).toHaveTextContent("semantic");
    expect(buildCard).toHaveTextContent("intfloat/e5-large-v2");
    expect(buildCard).toHaveTextContent("Object store + AGE");
    expect(buildCard).toHaveTextContent("Apache AGE graph");
    expect(buildCard).toHaveTextContent("balanced");
    expect(buildCard).toHaveTextContent("Waited for completion");
    expect(buildCard).toHaveTextContent("900s timeout");
    expect(buildCard).toHaveTextContent("Activated KBV-3");
    expect(buildCard).toHaveTextContent("AGE synced");
  });

  it("surfaces child workflow diagnostics with path, version, and token usage", () => {
    render(
      <StepPreview
        step={step({
          node_id: "child_workflow",
          node_type: "subworkflow",
          detail: "executed governed child workflow",
          output: "Escalated to the governed child workflow.",
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
        })}
      />,
    );

    const subworkflowCard = screen.getByTestId("step-preview-subworkflow");
    expect(subworkflowCard).toHaveTextContent("Child Workflow");
    expect(subworkflowCard).toHaveTextContent("Child completed");
    expect(subworkflowCard).toHaveTextContent("WF-child");
    expect(subworkflowCard).toHaveTextContent("Alias prod");
    expect(subworkflowCard).toHaveTextContent("WFV-child");
    expect(subworkflowCard).toHaveTextContent("v4");
    expect(subworkflowCard).toHaveTextContent("3 child steps");
    expect(subworkflowCard).toHaveTextContent("17 tokens");
    expect(subworkflowCard).toHaveTextContent(
      "Path: child_start -> child_review -> child_final",
    );
    expect(subworkflowCard).toHaveTextContent(
      "Child output: Escalated to the governed child workflow.",
    );
  });

  it("renders a derived summary for workflow-native storage and KB nodes when detail is empty", () => {
    render(
      <StepPreview
        step={step({
          node_id: "artifact_sink",
          node_type: "output_bucket",
          detail: "",
          output: "",
          output_by_port: {
            keys: ["runs/report-1.json", "runs/report-2.json"],
            metadata: {
              bucket: "artifacts",
              prefix: "runs",
            },
          },
        })}
      />,
    );

    expect(screen.getByTestId("step-preview-change")).toHaveTextContent(
      "wrote 2 objects to artifacts/runs",
    );
  });

  it("surfaces skipped bucket objects and truncation in the storage diagnostics card", () => {
    render(
      <StepPreview
        step={step({
          node_id: "input_bucket",
          node_type: "input_bucket",
          detail: "",
          output: "--- a.txt ---\nhello",
          output_by_port: {
            text: "--- a.txt ---\nhello",
            files: [
              {
                key: "docs/run1/a.txt",
                relative_path: "a.txt",
                bytes: 5,
                truncated: false,
                text: "hello",
              },
            ],
            metadata: {
              bucket: "docs",
              prefix: "run1/",
              recursive: true,
              encoding: "utf-8",
              object_count: 1,
              skipped_object_count: 1,
              truncated_file_list: true,
            },
          },
        })}
      />,
    );

    expect(screen.getByTestId("step-preview-change")).toHaveTextContent(
      "loaded 1 object from docs/run1/ (1 skipped)",
    );

    const storageCard = screen.getByTestId("step-preview-storage-node");
    expect(storageCard).toHaveTextContent("Storage I/O");
    expect(storageCard).toHaveTextContent("Bucket docs");
    expect(storageCard).toHaveTextContent("Prefix run1/");
    expect(storageCard).toHaveTextContent("Recursive");
    expect(storageCard).toHaveTextContent("utf-8");
    expect(storageCard).toHaveTextContent("Listing truncated");
    expect(storageCard).toHaveTextContent("Skipped 1");
    expect(storageCard).toHaveTextContent(
      "Skipped 1 unreadable object while preserving the readable entries.",
    );
    expect(storageCard).toHaveTextContent("a.txt");
    expect(storageCard).toHaveTextContent("5 bytes");
  });

  it("surfaces for-each loop diagnostics with item outcomes", () => {
    render(
      <StepPreview
        step={step({
          node_id: "fanout",
          node_type: "for_each",
          detail: "processed 3 item(s) via agent (1 failed)",
          output: "chunk-a summary\nchunk-b summary",
          output_by_port: {
            results: [
              {
                item: "chunk-a.md",
                output: "chunk-a summary",
                status: "ok",
                tool_calls: [{ tool: "summarize" }],
              },
              {
                item: "chunk-b.md",
                output: "",
                error: "rate limit exceeded",
                tool_calls: [],
              },
              {
                item: "chunk-c.md",
                output: "chunk-c summary",
                status: "ok",
                artifacts: ["summary.json"],
                tool_calls: [],
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
        })}
      />,
    );

    const loopCard = screen.getByTestId("step-preview-for-each");
    expect(loopCard).toHaveTextContent("Loop Orchestration");
    expect(loopCard).toHaveTextContent("For-each loop");
    expect(loopCard).toHaveTextContent("3 items");
    expect(loopCard).toHaveTextContent("Target summarize_agent");
    expect(loopCard).toHaveTextContent("1 failed");
    expect(loopCard).toHaveTextContent("Artifact bundle 2");
    expect(loopCard).toHaveTextContent("chunk-a.md");
    expect(loopCard).toHaveTextContent("chunk-a summary");
    expect(loopCard).toHaveTextContent("rate limit exceeded");
  });

  it("surfaces join diagnostics with merged keys", () => {
    render(
      <StepPreview
        step={step({
          node_id: "merge",
          node_type: "join",
          detail: "merged branch outputs",
          output: "combined answer",
          output_by_port: {
            output: "combined answer",
            merged: {
              policy: "refund coverage",
              notes: "handoff summary",
              response: "combined answer",
            },
          },
        })}
      />,
    );

    const joinCard = screen.getByTestId("step-preview-join");
    expect(joinCard).toHaveTextContent("Branch Merge");
    expect(joinCard).toHaveTextContent("3 merged ports");
    expect(joinCard).toHaveTextContent("policy");
    expect(joinCard).toHaveTextContent("notes");
    expect(joinCard).toHaveTextContent("response");
    expect(joinCard).toHaveTextContent("combined answer");
  });

  it("surfaces handled error-boundary diagnostics with compensation context", () => {
    render(
      <StepPreview
        step={step({
          node_id: "guard",
          node_type: "error_boundary",
          detail: "handled error: upstream timeout",
          output: "fallback answer",
          output_by_port: {
            output: "fallback answer",
            error: {
              message: "upstream timeout",
              target_node_id: "fetch_policy",
              target_node_type: "tool",
              compensation_node_id: "fallback_agent",
              compensation_node_type: "agent",
              compensation_outputs: {
                output: "fallback answer",
              },
              artifacts: {
                "fallback/log.txt": "timeout observed",
              },
            },
          },
        })}
      />,
    );

    const boundaryCard = screen.getByTestId("step-preview-error-boundary");
    expect(boundaryCard).toHaveTextContent("Failure Recovery");
    expect(boundaryCard).toHaveTextContent("Handled failure");
    expect(boundaryCard).toHaveTextContent("Protected fetch_policy");
    expect(boundaryCard).toHaveTextContent("Compensation fallback_agent");
    expect(boundaryCard).toHaveTextContent("Artifact bundle 1");
    expect(boundaryCard).toHaveTextContent("upstream timeout");
    expect(boundaryCard).toHaveTextContent("Recovery output: fallback answer");
  });
});

describe("describeStepChange — storage diagnostics branches (via diagnostics override)", () => {
  it("special-cases file_input with a path regardless of counts", () => {
    expect(
      describeStepChange(
        step({ node_type: "file_input" }),
        { storageNode: storageNodeDiag({ nodeType: "file_input", path: "a/b/report.json", count: 1 }) },
      ),
    ).toBe("read file report.json");
  });

  it("combines count label and location (loaded ... from ...)", () => {
    expect(
      describeStepChange(step({ node_type: "folder_input" }), {
        storageNode: storageNodeDiag({
          nodeType: "folder_input",
          direction: "input",
          path: "docs/",
          count: 3,
          skippedCount: 2,
        }),
      }),
    ).toBe("loaded 3 files from docs/ (2 skipped)");
  });

  it("joins bucket and prefix into a location for output writes", () => {
    expect(
      describeStepChange(step({ node_type: "output_bucket" }), {
        storageNode: storageNodeDiag({
          nodeType: "output_bucket",
          direction: "output",
          bucket: "art",
          prefix: "runs/",
          count: 2,
        }),
      }),
    ).toBe("wrote 2 objects to art/runs/");
  });

  it("falls back to count label alone when there is no location", () => {
    expect(
      describeStepChange(step({ node_type: "output_folder" }), {
        storageNode: storageNodeDiag({
          nodeType: "output_folder",
          direction: "output",
          count: 4,
        }),
      }),
    ).toBe("wrote 4 files");
  });

  it("falls back to location alone (pluralized unit) when there is no count", () => {
    expect(
      describeStepChange(step({ node_type: "input_bucket" }), {
        storageNode: storageNodeDiag({
          nodeType: "input_bucket",
          direction: "input",
          bucket: "docs",
          count: null,
        }),
      }),
    ).toBe("loaded objects from docs");
  });

  it("returns null (falls through to status) when neither count nor location exist", () => {
    expect(
      describeStepChange(step({ node_type: "folder_input", status: "ok" }), {
        storageNode: storageNodeDiag({ nodeType: "folder_input", direction: "input" }),
        knowledgeQuery: null,
        knowledgeBuild: null,
        forEachNode: null,
        joinNode: null,
        errorBoundary: null,
        subworkflowNode: null,
        toolNode: null,
      }),
    ).toBe("ok");
  });

  it("suffixes a storage-detail message with a skipped-only notice", () => {
    expect(
      describeStepChange(
        step({ detail: "read 2 object(s) from docs/" }),
        { storageNode: storageNodeDiag({ skippedCount: 3 }) },
      ),
    ).toBe("read 2 object(s) from docs/ (3 skipped)");
  });

  it("suffixes a storage-detail message with a truncated-only notice", () => {
    expect(
      describeStepChange(
        step({ detail: "read 2 object(s) from docs/" }),
        { storageNode: storageNodeDiag({ truncatedList: true }) },
      ),
    ).toBe("read 2 object(s) from docs/ (listing truncated)");
  });
});

describe("describeStepChange — knowledge-query diagnostics branches", () => {
  it("prefers chunk count with a retrieval-mode label (singular)", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_query" }), {
        knowledgeQuery: knowledgeQueryDiag({
          retrievalMode: "age_graph",
          chunks: [{ chunk_id: "c1", source_name: "s", source_key: "k", score: null, content: "", matched_entity_labels: [] }],
        }),
      }),
    ).toBe("retrieved 1 chunk via Apache AGE graph");
  });

  it("pluralizes chunk count with a retrieval-mode label", () => {
    const chunk = { chunk_id: "c1", source_name: "s", source_key: "k", score: null, content: "", matched_entity_labels: [] };
    expect(
      describeStepChange(step({ node_type: "knowledge_query" }), {
        knowledgeQuery: knowledgeQueryDiag({ retrievalMode: "dense", chunks: [chunk, chunk] }),
      }),
    ).toBe("retrieved 2 chunks via Dense chunks");
  });

  it("falls back to just the retrieval mode when there are no chunks/citations/results", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_query" }), {
        knowledgeQuery: knowledgeQueryDiag({ retrievalMode: "graph_hybrid" }),
      }),
    ).toBe("queried the knowledge base via GraphRAG hybrid");
  });

  it("falls back to a bare chunk count (singular) with no retrieval-mode label", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_query" }), {
        knowledgeQuery: knowledgeQueryDiag({
          chunks: [{ chunk_id: "c1", source_name: "s", source_key: "k", score: null, content: "", matched_entity_labels: [] }],
        }),
      }),
    ).toBe("retrieved 1 knowledge chunk");
  });

  it("falls back to a bare chunk count (plural) with no retrieval-mode label", () => {
    const chunk = { chunk_id: "c1", source_name: "s", source_key: "k", score: null, content: "", matched_entity_labels: [] };
    expect(
      describeStepChange(step({ node_type: "knowledge_query" }), {
        knowledgeQuery: knowledgeQueryDiag({ chunks: [chunk, chunk] }),
      }),
    ).toBe("retrieved 2 knowledge chunks");
  });

  it("returns null (falls through) when there is nothing to summarize", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_query", status: "ok" }), {
        knowledgeQuery: knowledgeQueryDiag(),
        knowledgeBuild: null,
        forEachNode: null,
        joinNode: null,
        errorBoundary: null,
        subworkflowNode: null,
        toolNode: null,
      }),
    ).toBe("ok");
  });
});

describe("describeStepChange — knowledge-build diagnostics branches", () => {
  it("reports a preview-skipped build without a knowledge base id", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ previewSkipped: true }),
      }),
    ).toBe("previewed knowledge base build");
  });

  it("reports a preview-skipped build with a knowledge base id", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ previewSkipped: true, knowledgeBaseId: "KB-1" }),
      }),
    ).toBe("previewed knowledge base KB-1 build");
  });

  it("reports queued/processing/failed/completed statuses", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ status: "queued", knowledgeBaseId: "KB-1" }),
      }),
    ).toBe("queued knowledge base KB-1 build");
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ status: "processing", knowledgeBaseId: "KB-1" }),
      }),
    ).toBe("building knowledge base KB-1");
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ status: "failed", knowledgeBaseId: "KB-1" }),
      }),
    ).toBe("failed knowledge base KB-1 build");
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ status: "completed", knowledgeBaseId: "KB-1", versionNumber: 2 }),
      }),
    ).toBe("built knowledge base KB-1 v2");
  });

  it("falls back to a 'prepared' summary for unrecognized statuses with a version id", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_build" }), {
        knowledgeBuild: knowledgeBuildDiag({ status: "archived", knowledgeBaseId: "KB-1", versionId: "V1" }),
      }),
    ).toBe("prepared knowledge base KB-1 V1");
  });

  it("returns null (falls through) with no status and no version id", () => {
    expect(
      describeStepChange(step({ node_type: "knowledge_build", status: "ok" }), {
        knowledgeBuild: knowledgeBuildDiag(),
        forEachNode: null,
        joinNode: null,
        errorBoundary: null,
        subworkflowNode: null,
        toolNode: null,
      }),
    ).toBe("ok");
  });
});

describe("describeStepChange — for-each/loop diagnostics branches", () => {
  it("returns null (falls through) when count is zero", () => {
    expect(
      describeStepChange(step({ node_type: "for_each", status: "ok" }), {
        forEachNode: forEachDiag({ count: 0 }),
        joinNode: null,
        errorBoundary: null,
        subworkflowNode: null,
        toolNode: null,
      }),
    ).toBe("ok");
  });

  it("uses loop wording (iteration/completed) for node_type loop", () => {
    expect(
      describeStepChange(step({ node_type: "loop" }), {
        forEachNode: forEachDiag({ count: 1 }),
      }),
    ).toBe("completed 1 iteration");
  });

  it("uses item wording with a target and failures for for_each", () => {
    expect(
      describeStepChange(step({ node_type: "for_each" }), {
        forEachNode: forEachDiag({ count: 5, targetNodeId: "x", failed: 2 }),
      }),
    ).toBe("processed 5 items via x (2 failed)");
  });
});

describe("describeStepChange — join diagnostics branches", () => {
  it("returns null (falls through) when branchCount is zero", () => {
    expect(
      describeStepChange(step({ node_type: "join", status: "ok" }), {
        joinNode: joinDiag({ branchCount: 0 }),
        errorBoundary: null,
        subworkflowNode: null,
        toolNode: null,
      }),
    ).toBe("ok");
  });

  it("pluralizes merged branches", () => {
    expect(
      describeStepChange(step({ node_type: "join" }), {
        joinNode: joinDiag({ branchCount: 1 }),
      }),
    ).toBe("merged 1 branch");
    expect(
      describeStepChange(step({ node_type: "join" }), {
        joinNode: joinDiag({ branchCount: 3 }),
      }),
    ).toBe("merged 3 branches");
  });
});

describe("describeStepChange — error-boundary diagnostics branches", () => {
  it("prefers the error message over the compensation node id", () => {
    expect(
      describeStepChange(step({ node_type: "error_boundary" }), {
        errorBoundary: errorBoundaryDiag({ message: "boom", compensationNodeId: "fallback" }),
      }),
    ).toBe("handled error: boom");
  });

  it("falls back to compensation node id when there is no message", () => {
    expect(
      describeStepChange(step({ node_type: "error_boundary" }), {
        errorBoundary: errorBoundaryDiag({ compensationNodeId: "fallback" }),
      }),
    ).toBe("ran compensation fallback");
  });

  it("returns null (falls through) with neither message nor compensation", () => {
    expect(
      describeStepChange(step({ node_type: "error_boundary", status: "ok" }), {
        errorBoundary: errorBoundaryDiag(),
        subworkflowNode: null,
        toolNode: null,
      }),
    ).toBe("ok");
  });
});

describe("describeStepChange — subworkflow diagnostics branches", () => {
  it("returns null (falls through) without a workflow id", () => {
    expect(
      describeStepChange(step({ node_type: "subworkflow", status: "ok" }), {
        subworkflowNode: subworkflowDiag(),
        toolNode: null,
      }),
    ).toBe("ok");
  });

  it("reports completed/blocked/error and a default 'ran' status with alias", () => {
    expect(
      describeStepChange(step({ node_type: "subworkflow" }), {
        subworkflowNode: subworkflowDiag({ workflowId: "WF", alias: "prod", childStatus: "completed" }),
      }),
    ).toBe("completed child workflow WF@prod");
    expect(
      describeStepChange(step({ node_type: "subworkflow" }), {
        subworkflowNode: subworkflowDiag({ workflowId: "WF", childStatus: "blocked" }),
      }),
    ).toBe("child workflow WF blocked");
    expect(
      describeStepChange(step({ node_type: "subworkflow" }), {
        subworkflowNode: subworkflowDiag({ workflowId: "WF", childStatus: "error" }),
      }),
    ).toBe("child workflow WF failed");
    expect(
      describeStepChange(step({ node_type: "subworkflow" }), {
        subworkflowNode: subworkflowDiag({ workflowId: "WF", childStatus: "running" }),
      }),
    ).toBe("ran child workflow WF");
  });
});

describe("describeStepChange — direct tool-node fallback", () => {
  it("names the tool when only tool-node diagnostics are present", () => {
    expect(
      describeStepChange(step({ node_type: "agent", tool_calls: [] }), {
        toolNode: toolNodeDiag({ localName: "lookup_policy" }),
      }),
    ).toBe("ran tool lookup_policy");
  });
});

describe("extractStorageNodeDiagnostics", () => {
  it("returns null for unsupported node types", () => {
    expect(extractStorageNodeDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null for file_input with no path and no metadata", () => {
    expect(
      extractStorageNodeDiagnostics(step({ node_type: "file_input", output_by_port: undefined })),
    ).toBeNull();
  });

  it("extracts file_input diagnostics from a path", () => {
    const diag = extractStorageNodeDiagnostics(
      step({
        node_type: "file_input",
        output_by_port: { path: "reports/out.json", metadata: { bytes: 128, truncated: true } },
      }),
    );
    expect(diag).not.toBeNull();
    expect(diag?.path).toBe("reports/out.json");
    expect(diag?.entries).toHaveLength(1);
    expect(diag?.entries[0]?.label).toBe("out.json");
    expect(diag?.entries[0]?.bytes).toBe(128);
    expect(diag?.entries[0]?.truncated).toBe(true);
  });

  it("returns null for folder_input with no path and no files", () => {
    expect(
      extractStorageNodeDiagnostics(step({ node_type: "folder_input", output_by_port: {} })),
    ).toBeNull();
  });

  it("falls back to files.length and a string file_count for folder_input counts", () => {
    const diag = extractStorageNodeDiagnostics(
      step({
        node_type: "folder_input",
        output_by_port: {
          files: [{ relative_path: "a.txt" }, { relative_path: "b.txt" }],
          metadata: { file_count: "2", matched_count: 5, path: "data/" },
        },
      }),
    );
    expect(diag?.count).toBe(2);
    expect(diag?.matchedCount).toBe(5);
    expect(diag?.entries.map((e) => e.label)).toEqual(["a.txt", "b.txt"]);
  });

  it("returns diagnostics for input_bucket even without a bucket name when entries exist", () => {
    const diag = extractStorageNodeDiagnostics(
      step({
        node_type: "input_bucket",
        output_by_port: { files: [{ key: "a" }] },
      }),
    );
    expect(diag).not.toBeNull();
    expect(diag?.bucket).toBeNull();
  });

  it("returns null for input_bucket with no bucket and no entries", () => {
    expect(
      extractStorageNodeDiagnostics(step({ node_type: "input_bucket", output_by_port: {} })),
    ).toBeNull();
  });

  it("falls back from ports.keys to metadata.keys for output_bucket", () => {
    const diag = extractStorageNodeDiagnostics(
      step({
        node_type: "output_bucket",
        output_by_port: { keys: [], metadata: { keys: ["runs/a.json"], bucket: "artifacts" } },
      }),
    );
    expect(diag?.entries.map((e) => e.label)).toEqual(["a.json"]);
  });

  it("falls back from ports.files to metadata.files for output_folder", () => {
    const diag = extractStorageNodeDiagnostics(
      step({
        node_type: "output_folder",
        output_by_port: { files: [], metadata: { files: ["out/a.json"], path: "out/" } },
      }),
    );
    expect(diag?.entries.map((e) => e.label)).toEqual(["a.json"]);
    expect(diag?.path).toBe("out/");
  });
});

describe("extractKnowledgeQueryDiagnostics", () => {
  it("returns null for a non-knowledge_query step", () => {
    expect(extractKnowledgeQueryDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when output_by_port is missing", () => {
    expect(
      extractKnowledgeQueryDiagnostics(step({ node_type: "knowledge_query", output_by_port: undefined })),
    ).toBeNull();
  });

  it("returns null when nothing in the ports matches a knowledge shape", () => {
    expect(
      extractKnowledgeQueryDiagnostics(step({ node_type: "knowledge_query", output_by_port: {} })),
    ).toBeNull();
  });

  it("falls back to the primary version's graph_context, citations, and retrieved_chunks", () => {
    const diag = extractKnowledgeQueryDiagnostics(
      step({
        node_type: "knowledge_query",
        output_by_port: {
          result: {
            versions: [
              {
                retrieval_mode: "dense",
                graph_context: { matched_entities: ["Bob"] },
                citations: [{ chunk_id: "c1" }],
                retrieved_chunks: [{ chunk_id: "ch1" }],
              },
            ],
          },
        },
      }),
    );
    expect(diag?.retrievalMode).toBe("dense");
    expect(diag?.matchedEntities).toEqual(["Bob"]);
    expect(diag?.citations[0]?.chunk_id).toBe("c1");
    expect(diag?.chunks[0]?.chunk_id).toBe("ch1");
  });

  it("defaults citation/chunk labels and ids when fields are absent", () => {
    const diag = extractKnowledgeQueryDiagnostics(
      step({
        node_type: "knowledge_query",
        output_by_port: {
          citations: [{}],
          chunks: [{}],
        },
      }),
    );
    expect(diag?.citations[0]).toEqual({ chunk_id: "citation-0", label: "Citation 1" });
    expect(diag?.chunks[0]?.chunk_id).toBe("chunk-0");
    expect(diag?.chunks[0]?.source_name).toBe("Chunk 1");
    expect(diag?.chunks[0]?.source_key).toBe("");
    expect(diag?.chunks[0]?.score).toBeNull();
    expect(diag?.chunks[0]?.content).toBe("");
    expect(diag?.chunks[0]?.matched_entity_labels).toEqual([]);
  });
});

describe("extractKnowledgeBuildDiagnostics", () => {
  it("returns null for a non-knowledge_build step", () => {
    expect(extractKnowledgeBuildDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when output_by_port is missing", () => {
    expect(
      extractKnowledgeBuildDiagnostics(step({ node_type: "knowledge_build", output_by_port: undefined })),
    ).toBeNull();
  });

  it("returns null when every extracted field is empty", () => {
    expect(
      extractKnowledgeBuildDiagnostics(step({ node_type: "knowledge_build", output_by_port: {} })),
    ).toBeNull();
  });

  it("treats a boolean preview flag as preview-skipped", () => {
    const diag = extractKnowledgeBuildDiagnostics(
      step({
        node_type: "knowledge_build",
        output_by_port: { result: { preview: true, knowledge_base_id: "KB-9" } },
      }),
    );
    expect(diag?.previewSkipped).toBe(true);
    expect(diag?.knowledgeBaseId).toBe("KB-9");
  });

  it("falls back status resolution across ports, result, and version", () => {
    const diag = extractKnowledgeBuildDiagnostics(
      step({
        node_type: "knowledge_build",
        output_by_port: { version: { status: "processing", knowledge_base_version_id: "KBV-1" } },
      }),
    );
    expect(diag?.status).toBe("processing");
    expect(diag?.versionId).toBe("KBV-1");
  });
});

describe("extractToolNodeDiagnostics", () => {
  it("returns null for a non-tool step", () => {
    expect(extractToolNodeDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when nothing is present to describe", () => {
    expect(
      extractToolNodeDiagnostics(step({ node_type: "tool", output: "", output_by_port: undefined })),
    ).toBeNull();
  });

  it("prefers port-level tool_calls over step-level tool_calls", () => {
    const diag = extractToolNodeDiagnostics(
      step({
        node_type: "tool",
        tool_calls: [{ tool: "step_level" }],
        output_by_port: { tool_calls: [{ tool: "port_a" }, { tool: "port_b" }] },
      }),
    );
    expect(diag?.callCount).toBe(2);
    expect(diag?.localName).toBe("port_a");
  });

  it("falls back to the primary call's arguments when metadata lacks them", () => {
    const diag = extractToolNodeDiagnostics(
      step({
        node_type: "tool",
        output_by_port: {
          tool_calls: [{ tool: "lookup", arguments: { a: 1, b: 2 } }],
        },
      }),
    );
    expect(diag?.argumentKeys).toEqual(["a", "b"]);
  });

  it("falls back to step.output for the result preview when ports are absent", () => {
    const diag = extractToolNodeDiagnostics(
      step({ node_type: "tool", output: "direct output text" }),
    );
    expect(diag?.resultPreview).toBe("direct output text");
  });
});

describe("extractForEachDiagnostics", () => {
  it("returns null for a node_type that is neither for_each nor loop", () => {
    expect(extractForEachDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when output_by_port is missing", () => {
    expect(
      extractForEachDiagnostics(step({ node_type: "for_each", output_by_port: undefined })),
    ).toBeNull();
  });

  it("returns null when every extracted field is empty", () => {
    expect(extractForEachDiagnostics(step({ node_type: "for_each", output_by_port: {} }))).toBeNull();
  });

  it("supports node_type loop", () => {
    const diag = extractForEachDiagnostics(
      step({ node_type: "loop", output_by_port: { results: [{ item: "a" }] } }),
    );
    expect(diag).not.toBeNull();
  });

  it("derives count and failed from results when metadata is absent", () => {
    const diag = extractForEachDiagnostics(
      step({
        node_type: "for_each",
        output_by_port: {
          results: [{ item: "a" }, { item: "b", error: "boom" }],
        },
      }),
    );
    expect(diag?.count).toBe(2);
    expect(diag?.failed).toBe(1);
  });

  it("labels each result's status: explicit, error-implied, and default ok", () => {
    const diag = extractForEachDiagnostics(
      step({
        node_type: "for_each",
        output_by_port: {
          results: [
            { item: "a", status: "skipped" },
            { item: "b", error: "boom" },
            { item: "c" },
          ],
        },
      }),
    );
    expect(diag?.results.map((r) => r.status)).toEqual(["skipped", "error", "ok"]);
  });

  it("caps rendered result previews at 3 even with more results", () => {
    const diag = extractForEachDiagnostics(
      step({
        node_type: "for_each",
        output_by_port: {
          results: [{ item: "a" }, { item: "b" }, { item: "c" }, { item: "d" }],
        },
      }),
    );
    expect(diag?.results).toHaveLength(3);
    expect(diag?.count).toBe(4);
  });

  it("labels items by type: number, boolean, and array", () => {
    const diag = extractForEachDiagnostics(
      step({
        node_type: "for_each",
        output_by_port: {
          results: [{ item: 42 }, { item: true }, { item: [1, 2, 3] }],
        },
      }),
    );
    expect(diag?.results.map((r) => r.itemLabel)).toEqual(["42", "true", "list(3)"]);
  });

  it("labels a record item by its name field, and falls back to a compact JSON label otherwise", () => {
    const diag = extractForEachDiagnostics(
      step({
        node_type: "for_each",
        output_by_port: {
          results: [{ item: { name: "widget-7" } }, { item: { unrelated: "field" } }],
        },
      }),
    );
    expect(diag?.results.map((r) => r.itemLabel)).toEqual([
      "widget-7",
      '{"unrelated":"field"}',
    ]);
  });

  it("truncates a long item output preview beyond 180 characters", () => {
    const longOutput = "x".repeat(200);
    const diag = extractForEachDiagnostics(
      step({
        node_type: "for_each",
        output_by_port: { results: [{ item: "a", output: longOutput }] },
      }),
    );
    expect(diag?.results[0]?.outputPreview).toHaveLength(180);
    expect(diag?.results[0]?.outputPreview).toMatch(/\.\.\.$/);
  });
});

describe("extractJoinDiagnostics", () => {
  it("returns null for a non-join step", () => {
    expect(extractJoinDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when output_by_port is missing", () => {
    expect(extractJoinDiagnostics(step({ node_type: "join", output_by_port: undefined }))).toBeNull();
  });

  it("returns null when there is no merged object and no output", () => {
    expect(
      extractJoinDiagnostics(step({ node_type: "join", output: "", output_by_port: {} })),
    ).toBeNull();
  });

  it("sorts merged keys alphabetically", () => {
    const diag = extractJoinDiagnostics(
      step({
        node_type: "join",
        output_by_port: { merged: { zeta: "z", alpha: "a" } },
      }),
    );
    expect(diag?.mergedKeys).toEqual(["alpha", "zeta"]);
  });

  it("keeps a non-null result when only an output preview exists (zero branches)", () => {
    const diag = extractJoinDiagnostics(
      step({ node_type: "join", output_by_port: { output: "combined" } }),
    );
    expect(diag?.branchCount).toBe(0);
    expect(diag?.outputPreview).toBe("combined");
  });
});

describe("extractErrorBoundaryDiagnostics", () => {
  it("returns null for a non-error_boundary step", () => {
    expect(extractErrorBoundaryDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when output_by_port is missing", () => {
    expect(
      extractErrorBoundaryDiagnostics(step({ node_type: "error_boundary", output_by_port: undefined })),
    ).toBeNull();
  });

  it("returns null when the error payload is absent", () => {
    expect(
      extractErrorBoundaryDiagnostics(step({ node_type: "error_boundary", output_by_port: {} })),
    ).toBeNull();
  });

  it("counts artifacts in the error payload", () => {
    const diag = extractErrorBoundaryDiagnostics(
      step({
        node_type: "error_boundary",
        output_by_port: { error: { message: "x", artifacts: { a: "1", b: "2" } } },
      }),
    );
    expect(diag?.artifactCount).toBe(2);
  });
});

describe("extractSubworkflowDiagnostics", () => {
  it("returns null for a non-subworkflow step", () => {
    expect(extractSubworkflowDiagnostics(step({ node_type: "agent" }))).toBeNull();
  });

  it("returns null when there is no meaningful result data", () => {
    expect(
      extractSubworkflowDiagnostics(step({ node_type: "subworkflow", output: "", output_by_port: {} })),
    ).toBeNull();
  });

  it("falls back to version_id/version_number when the *_workflow_version_* fields are absent", () => {
    const diag = extractSubworkflowDiagnostics(
      step({
        node_type: "subworkflow",
        output_by_port: {
          result: { workflow_id: "WF", version_id: "V9", version_number: 7 },
        },
      }),
    );
    expect(diag?.workflowVersionId).toBe("V9");
    expect(diag?.workflowVersionNumber).toBe(7);
  });
});

describe("extractStepTelemetry", () => {
  it("returns null when no telemetry fields are present", () => {
    expect(extractStepTelemetry(step())).toBeNull();
  });

  it("returns a populated object when at least one field is present", () => {
    expect(extractStepTelemetry(step({ tokens: 10 }))).not.toBeNull();
  });
});

describe("pure label/formatting helpers", () => {
  it("formatUsdEstimate covers the zero, tiny, and tiered-precision branches", () => {
    expect(formatUsdEstimate(0)).toBe("$0.00");
    expect(formatUsdEstimate(-1)).toBe("$0.00");
    expect(formatUsdEstimate(0.0000001)).toBe("<$0.000001");
    expect(formatUsdEstimate(1.5)).toBe("$1.50");
    expect(formatUsdEstimate(0.05)).toBe("$0.0500");
    expect(formatUsdEstimate(0.000123)).toBe("$0.000123");
  });

  it("storageItemUnit distinguishes bucket nodes from file nodes", () => {
    expect(storageItemUnit("input_bucket")).toBe("object");
    expect(storageItemUnit("output_bucket")).toBe("object");
    expect(storageItemUnit("file_input")).toBe("file");
    expect(storageItemUnit("folder_input")).toBe("file");
  });

  it("storageCountLabel pluralizes and handles null", () => {
    expect(storageCountLabel("file_input", null)).toBeNull();
    expect(storageCountLabel("file_input", 1)).toBe("1 file");
    expect(storageCountLabel("input_bucket", 2)).toBe("2 objects");
  });

  it("toolArgumentSummary covers zero, one, and many keys", () => {
    expect(toolArgumentSummary([])).toBe("No explicit arguments recorded");
    expect(toolArgumentSummary(["a"])).toBe("1 key: a");
    expect(toolArgumentSummary(["a", "b"])).toBe("2 keys: a, b");
  });

  it("toolBindingTargetLabel covers module, remote, server-only, and null", () => {
    expect(toolBindingTargetLabel(null)).toBeNull();
    expect(
      toolBindingTargetLabel(toolNodeDiag({ modulePath: "pkg.mod", callableName: "fn" })),
    ).toBe("pkg.mod:fn");
    expect(
      toolBindingTargetLabel(toolNodeDiag({ serverId: "srv", remoteToolName: "tool_x" })),
    ).toBe("srv/tool_x");
    expect(toolBindingTargetLabel(toolNodeDiag({ serverId: "srv" }))).toBe("srv");
    expect(toolBindingTargetLabel(toolNodeDiag())).toBeNull();
  });

  it("retrievalModeLabel maps known modes and passes through unknown ones", () => {
    expect(retrievalModeLabel(null)).toBeNull();
    expect(retrievalModeLabel("age_graph")).toBe("Apache AGE graph");
    expect(retrievalModeLabel("graph_hybrid")).toBe("GraphRAG hybrid");
    expect(retrievalModeLabel("dense")).toBe("Dense chunks");
    expect(retrievalModeLabel("custom_mode")).toBe("custom_mode");
  });

  it("ageSeedStrategyLabel maps known strategies and returns null otherwise", () => {
    expect(ageSeedStrategyLabel("query_text")).toBe("Seeded from question text");
    expect(ageSeedStrategyLabel("query_entities")).toBe("Seeded from extracted entities");
    expect(ageSeedStrategyLabel("query_entities_and_text")).toBe(
      "Seeded from entities + question text",
    );
    expect(ageSeedStrategyLabel("other")).toBeNull();
    expect(ageSeedStrategyLabel(null)).toBeNull();
  });

  it("knowledgeGraphTargetLabel maps known targets and humanizes unknown ones", () => {
    expect(knowledgeGraphTargetLabel(null)).toBeNull();
    expect(knowledgeGraphTargetLabel("object_store_and_age")).toBe("Object store + AGE");
    expect(knowledgeGraphTargetLabel("object_store")).toBe("Object store");
    expect(knowledgeGraphTargetLabel("age_only")).toBe("age only");
  });

  it("knowledgeBuildStatusLabel covers preview-skipped, known, unknown, and null", () => {
    expect(knowledgeBuildStatusLabel(null, true)).toBe("Preview skipped");
    expect(knowledgeBuildStatusLabel("preview_skipped")).toBe("Preview skipped");
    expect(knowledgeBuildStatusLabel(null)).toBeNull();
    expect(knowledgeBuildStatusLabel("queued")).toBe("Build queued");
    expect(knowledgeBuildStatusLabel("processing")).toBe("Build processing");
    expect(knowledgeBuildStatusLabel("completed")).toBe("Build completed");
    expect(knowledgeBuildStatusLabel("failed")).toBe("Build failed");
    expect(knowledgeBuildStatusLabel("archived")).toBe("Build archived");
  });

  it("knowledgeBuildWaitStatusLabel covers requested/completed/timeout/not_requested/unknown/null", () => {
    expect(knowledgeBuildWaitStatusLabel(null, false)).toBeNull();
    expect(knowledgeBuildWaitStatusLabel("completed", true)).toBe("Waited for completion");
    expect(knowledgeBuildWaitStatusLabel("timeout", true)).toBe("Wait timed out");
    expect(knowledgeBuildWaitStatusLabel("not_requested", false)).toBe("Did not wait");
    expect(knowledgeBuildWaitStatusLabel(null, true)).toBe("Waiting requested");
    expect(knowledgeBuildWaitStatusLabel("odd_state", true)).toBe("Wait odd state");
  });

  it("knowledgeBuildActivationStatusLabel covers activated/pending/skipped/requested/unknown/null", () => {
    expect(knowledgeBuildActivationStatusLabel(null, null, false)).toBeNull();
    expect(knowledgeBuildActivationStatusLabel("activated", "KBV-1", true)).toBe("Activated KBV-1");
    expect(knowledgeBuildActivationStatusLabel("activated", null, true)).toBe("Activated");
    expect(knowledgeBuildActivationStatusLabel("pending", null, true)).toBe("Activation deferred");
    expect(knowledgeBuildActivationStatusLabel("skipped", null, true)).toBe("Activation skipped");
    expect(knowledgeBuildActivationStatusLabel(null, null, true)).toBe("Activation requested");
    expect(knowledgeBuildActivationStatusLabel("odd_state", null, true)).toBe(
      "Activation odd state",
    );
  });

  it("toolBindingTypeLabel maps known bindings and humanizes unknown ones", () => {
    expect(toolBindingTypeLabel(null)).toBeNull();
    expect(toolBindingTypeLabel("registered_function")).toBe("Registered function");
    expect(toolBindingTypeLabel("mcp_tool")).toBe("MCP tool");
    expect(toolBindingTypeLabel("custom_binding")).toBe("custom binding");
  });

  it("toolSideEffectLabel maps external_action and humanizes unknown ones", () => {
    expect(toolSideEffectLabel(null)).toBeNull();
    expect(toolSideEffectLabel("external_action")).toBe("external action");
    expect(toolSideEffectLabel("read_only")).toBe("read only");
  });

  it("subworkflowStatusLabel covers completed/blocked/error/unknown/null", () => {
    expect(subworkflowStatusLabel(null)).toBeNull();
    expect(subworkflowStatusLabel("completed")).toBe("Child completed");
    expect(subworkflowStatusLabel("blocked")).toBe("Child blocked");
    expect(subworkflowStatusLabel("error")).toBe("Child failed");
    expect(subworkflowStatusLabel("running")).toBe("Child running");
  });

  it("subworkflowStatusTone covers completed/blocked/other-truthy/null", () => {
    expect(subworkflowStatusTone("completed")).toContain("emerald");
    expect(subworkflowStatusTone("blocked")).toContain("amber");
    expect(subworkflowStatusTone("error")).toContain("rose");
    expect(subworkflowStatusTone(null)).toContain("zinc");
  });
});

describe("StepPreview — additional prop-driven render branches", () => {
  it("renders no input details when there is no upstream output", () => {
    render(<StepPreview step={step()} upstream={[]} />);
    expect(screen.queryByTestId("step-preview-input")).not.toBeInTheDocument();
  });

  it("renders no telemetry card when no telemetry fields are present", () => {
    render(<StepPreview step={step()} />);
    expect(screen.queryByTestId("step-preview-telemetry")).not.toBeInTheDocument();
  });

  it("renders file_input storage diagnostics with byte size and truncation", () => {
    render(
      <StepPreview
        step={step({
          node_id: "read_report",
          node_type: "file_input",
          detail: "",
          output: "report contents",
          output_by_port: {
            path: "reports/out.json",
            metadata: { bytes: 128, truncated: true, encoding: "utf-8" },
          },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-change")).toHaveTextContent(
      "read file out.json",
    );
    const card = screen.getByTestId("step-preview-storage-node");
    expect(card).toHaveTextContent("File input");
    expect(card).toHaveTextContent("1 file");
    expect(card).toHaveTextContent("Location:");
    expect(card).toHaveTextContent("reports/out.json");
    expect(card).toHaveTextContent("128 bytes");
    expect(card).toHaveTextContent("Truncated");
    expect(card).toHaveTextContent("out.json");
  });

  it("renders folder_input storage diagnostics with pattern, recursion, and a matched-vs-count mismatch", () => {
    render(
      <StepPreview
        step={step({
          node_id: "scan_folder",
          node_type: "folder_input",
          detail: "",
          output: "",
          output_by_port: {
            files: [{ relative_path: "a.txt" }, { relative_path: "b.txt" }],
            metadata: {
              path: "data/",
              pattern: "*.txt",
              recursive: true,
              encoding: "utf-8",
              file_count: 2,
              matched_count: 5,
              truncated_file_list: true,
            },
          },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-change")).toHaveTextContent(
      "loaded 2 files from data/",
    );
    const card = screen.getByTestId("step-preview-storage-node");
    expect(card).toHaveTextContent("Pattern *.txt");
    expect(card).toHaveTextContent("Recursive");
    expect(card).toHaveTextContent("Listing truncated");
    expect(card).toHaveTextContent("Matched 5 total files.");
    expect(card).not.toHaveTextContent("Skipped");
  });

  it("renders output_folder storage diagnostics via the generic files fallback", () => {
    render(
      <StepPreview
        step={step({
          node_id: "write_folder",
          node_type: "output_folder",
          detail: "",
          output: "",
          output_by_port: {
            files: ["out/a.json", "out/b.json"],
            metadata: { path: "out/" },
          },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-change")).toHaveTextContent(
      "wrote 2 files to out/",
    );
    const card = screen.getByTestId("step-preview-storage-node");
    expect(card).toHaveTextContent("Output folder");
    expect(card).toHaveTextContent("a.json");
  });

  it("renders a minimal knowledge-query card with dense mode and no graph context", () => {
    render(
      <StepPreview
        step={step({
          node_id: "kq",
          node_type: "knowledge_query",
          output: "answer",
          output_by_port: {
            chunks: [
              {
                chunk_id: "c1",
                source_name: "doc",
                source_key: "",
                score: null,
                content: "some retrieved content",
                matched_entity_labels: [],
              },
            ],
            graph_context: { expanded_entities: ["Widget"] },
            result: { versions: [{ retrieval_mode: "dense" }] },
          },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-knowledge-query");
    expect(card).toHaveTextContent("Dense chunks");
    expect(card).not.toHaveTextContent("Strict AGE");
    expect(card).toHaveTextContent("No direct matches");
    expect(card).toHaveTextContent("Widget");
    expect(screen.queryByTestId("step-preview-knowledge-citations")).not.toBeInTheDocument();
    expect(card).not.toHaveTextContent("Graph tuning:");
    expect(card).not.toHaveTextContent("before rerank");
    expect(card).toHaveTextContent("some retrieved content");
  });

  it("renders a knowledge-query card in graph_hybrid mode", () => {
    render(
      <StepPreview
        step={step({
          node_type: "knowledge_query",
          output_by_port: {
            chunks: [
              {
                chunk_id: "c1",
                source_name: "doc",
                content: "hybrid content",
              },
            ],
            result: { versions: [{ retrieval_mode: "graph_hybrid" }] },
          },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-knowledge-query")).toHaveTextContent(
      "GraphRAG hybrid",
    );
  });

  it("renders a knowledge-build card with an unrecognized status tone", () => {
    render(
      <StepPreview
        step={step({
          node_type: "knowledge_build",
          output_by_port: { result: { status: "archived", knowledge_base_id: "KB-1" } },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-knowledge-build")).toHaveTextContent(
      "Build archived",
    );
  });

  it("renders knowledge-build cards for queued, processing, failed, and preview-skipped statuses", () => {
    const { unmount: u1 } = render(
      <StepPreview
        step={step({
          node_type: "knowledge_build",
          output_by_port: { result: { status: "queued", knowledge_base: { knowledge_base_id: "KB-1" } } },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-knowledge-build")).toHaveTextContent("Build queued");
    expect(screen.getByTestId("step-preview-knowledge-build")).not.toHaveTextContent("Build profile");
    expect(screen.getByTestId("step-preview-knowledge-build")).not.toHaveTextContent("Wait policy");
    expect(screen.getByTestId("step-preview-knowledge-build")).not.toHaveTextContent("Activation:");
    u1();

    const { unmount: u2 } = render(
      <StepPreview
        step={step({
          node_type: "knowledge_build",
          output_by_port: { result: { status: "processing" } },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-knowledge-build")).toHaveTextContent(
      "Build processing",
    );
    u2();

    const { unmount: u3 } = render(
      <StepPreview
        step={step({
          node_type: "knowledge_build",
          output_by_port: { result: { status: "failed" } },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-knowledge-build")).toHaveTextContent("Build failed");
    u3();

    render(
      <StepPreview
        step={step({
          node_type: "knowledge_build",
          output_by_port: { result: { status: "preview_skipped" } },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-knowledge-build")).toHaveTextContent(
      "Preview skipped",
    );
  });

  it("renders a minimal 'direct tool node' fallback when only step.output is available", () => {
    render(
      <StepPreview
        step={step({
          node_type: "tool",
          output: "raw tool output text",
          output_by_port: undefined,
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-tool-node");
    expect(card).toHaveTextContent("Direct tool node");
    expect(card).toHaveTextContent("Runtime-managed binding");
    expect(card).toHaveTextContent("No explicit arguments recorded");
    expect(card).toHaveTextContent("raw tool output text");
    expect(card).not.toHaveTextContent("calls");
    expect(card).not.toHaveTextContent("Approval required");
  });

  it("renders remote MCP tool binding target and a multi-call badge", () => {
    render(
      <StepPreview
        step={step({
          node_type: "tool",
          output_by_port: {
            metadata: { server_id: "srv-1", remote_tool_name: "remote_fn", tool_name: "remote_fn" },
            tool_calls: [{ tool: "remote_fn" }, { tool: "remote_fn" }],
          },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-tool-node");
    expect(card).toHaveTextContent("2 calls");
    expect(card).toHaveTextContent("srv-1/remote_fn");
  });

  it("renders a server-only binding target when no remote tool name is present", () => {
    render(
      <StepPreview
        step={step({
          node_type: "tool",
          output_by_port: { metadata: { server_id: "srv-only", tool_name: "x" } },
        })}
      />,
    );
    expect(screen.getByTestId("step-preview-tool-node")).toHaveTextContent("srv-only");
  });

  it("renders a for-each loop with no target, no failures, and a default-status item", () => {
    render(
      <StepPreview
        step={step({
          node_id: "fanout2",
          node_type: "for_each",
          output_by_port: {
            results: [{ item: "solo.md" }],
            metadata: { count: 1 },
          },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-for-each");
    expect(card).toHaveTextContent("1 item");
    expect(card).not.toHaveTextContent("Target");
    expect(card).not.toHaveTextContent("failed");
    expect(card).not.toHaveTextContent("Artifact bundle");
    expect(card).toHaveTextContent("solo.md");
    expect(card).toHaveTextContent("ok");
  });

  it("renders a join card with zero merged branches but an output preview", () => {
    render(
      <StepPreview
        step={step({
          node_id: "merge2",
          node_type: "join",
          detail: "",
          output_by_port: { output: "solo answer" },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-join");
    expect(card).toHaveTextContent("0 merged ports");
    expect(card).not.toHaveTextContent("Merged keys");
    expect(card).toHaveTextContent("solo answer");
  });

  it("renders an error-boundary card with only compensation context (no message)", () => {
    render(
      <StepPreview
        step={step({
          node_id: "guard2",
          node_type: "error_boundary",
          detail: "ran compensation fallback_x",
          output_by_port: {
            error: {
              compensation_node_id: "fallback_x",
              compensation_outputs: { output: "recovered" },
            },
          },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-error-boundary");
    expect(card).toHaveTextContent("Compensation fallback_x");
    expect(card).not.toHaveTextContent("Protected");
    expect(card).not.toHaveTextContent("Artifact bundle");
    expect(card).toHaveTextContent("Recovery output: recovered");
  });

  it("renders a blocked subworkflow with minimal fields (no alias/version/steps/tokens)", () => {
    render(
      <StepPreview
        step={step({
          node_id: "child_blocked",
          node_type: "subworkflow",
          output: "",
          output_by_port: { result: { status: "blocked", workflow_id: "WF-b" } },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-subworkflow");
    expect(card).toHaveTextContent("Child blocked");
    expect(card).not.toHaveTextContent("Alias");
    expect(card).not.toHaveTextContent("child step");
    expect(card).not.toHaveTextContent("tokens");
    expect(card).not.toHaveTextContent("Version:");
    expect(card).not.toHaveTextContent("Path:");
    expect(card).not.toHaveTextContent("Failure:");
  });

  it("renders an error child workflow with a failure message", () => {
    render(
      <StepPreview
        step={step({
          node_id: "child_error",
          node_type: "subworkflow",
          output_by_port: {
            result: { status: "error", workflow_id: "WF-e", error: "boom" },
          },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-subworkflow");
    expect(card).toHaveTextContent("Child failed");
    expect(card).toHaveTextContent("Failure: boom");
  });

  it("renders an unrecognized child status with a version-number-only version line and no status badge otherwise", () => {
    render(
      <StepPreview
        step={step({
          node_id: "child_running",
          node_type: "subworkflow",
          output_by_port: {
            result: { status: "running", workflow_id: "WF-r", workflow_version_number: 9 },
          },
        })}
      />,
    );
    const card = screen.getByTestId("step-preview-subworkflow");
    expect(card).toHaveTextContent("Child running");
    expect(card).toHaveTextContent("Version:");
    expect(card).toHaveTextContent("n/a");
    expect(card).toHaveTextContent("v9");
  });
});
