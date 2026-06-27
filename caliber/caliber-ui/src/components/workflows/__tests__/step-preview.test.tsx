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
