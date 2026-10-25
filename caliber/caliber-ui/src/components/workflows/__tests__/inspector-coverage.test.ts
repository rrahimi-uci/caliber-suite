import { describe, expect, it } from "vitest";

import {
  coerceWorkflowToolBinding,
  aliasChipTestId,
  compactGraphOverrides,
  fieldConstraintTokens,
  fieldMetaTokens,
  formatFieldMetaValue,
  formatJsonTextareaValue,
  formatTraceGroupTagsText,
  hasAgentStructuredOutputPort,
  hasMeaningfulFieldMetaValue,
  knowledgeAgeStatusLabel,
  knowledgeGraphTargetLabel,
  knowledgeRetrievalModeLabel,
  knowledgeResolvedDefaultRetrievalMode,
  knowledgeSummaryResolvedDefaultRetrievalMode,
  knowledgeVersionAgeConfigured,
  knowledgeVersionAgeReady,
  knowledgeVersionAgeSyncStatus,
  normalizeWorkflowRuntimeOpenAIConfig,
  parseNonNegativeInteger,
  parseNullablePositiveNumber,
  parseOptionalNumber,
  parseTraceGroupTagsText,
  pruneToolConstraints,
  targetNodeOptions,
  targetOptionLabel,
  uniqueTrimmedStrings,
  workflowDeployGateTestId,
  workflowDeployGateEntries,
  workflowArtifacts,
  workflowEvalDatasetArtifacts,
  workflowManifestStartTrigger,
  workflowPromptArtifacts,
  workflowPromptArtifactEntries,
  workflowPromptArtifactReferences,
  workflowPromptArtifactTestId,
  workflowRuntimeConfig,
  workflowRuntimeOpenAIConfig,
  workflowSessionMode,
  workflowToolBindingEntries,
  workflowToolBindingReferences,
  workflowToolBindingTestId,
  workflowToolBindingTypeLabel,
  subworkflowAliasTestId,
  subworkflowResolvedVersion,
  subworkflowWorkflowLabel,
  startTriggerAliasTestId,
  workflowTriggerSummary,
  workflowValidationCounts,
} from "@/components/workflows/Inspector";

describe("Inspector decision helpers", () => {
  it("normalizes runtime and knowledge configuration states", () => {
    expect(workflowSessionMode({ runtime: { session: { type: "in_memory" } } } as never)).toBe("in_memory");
    expect(workflowSessionMode({ runtime: { session: { type: "persistent" } } } as never)).toBe("persistent");
    expect(workflowSessionMode({ runtime: { session: { type: "other" } } } as never)).toBe("none");
    expect(workflowRuntimeConfig({} as never)).toEqual({});
    expect(workflowRuntimeConfig({ runtime: { openai: { workflow_api: "responses" } } } as never)).toEqual({ openai: { workflow_api: "responses" } });
    expect(workflowRuntimeOpenAIConfig({})).toEqual({});
    expect(workflowRuntimeOpenAIConfig({ openai: { workflow_api: "responses" } } as never)).toEqual({ workflow_api: "responses" });
    expect(normalizeWorkflowRuntimeOpenAIConfig({})).toBeUndefined();
    expect(normalizeWorkflowRuntimeOpenAIConfig({
      workflow_api: "responses", parallel_tool_calls: "true", prompt_cache_mode: "auto", prompt_cache_retention: "long",
    })).toEqual({ workflow_api: "responses", parallel_tool_calls: "true", prompt_cache_mode: "auto", prompt_cache_retention: "long" });
    expect(normalizeWorkflowRuntimeOpenAIConfig({ workflow_api: "", parallel_tool_calls: 1 } as never)).toBeUndefined();

    const ready = { graph_config: { output_target: "object_store_and_age" }, summary: { age_sync_status: "SYNCED" } };
    expect(knowledgeVersionAgeSyncStatus(ready)).toBe("synced");
    expect(knowledgeVersionAgeSyncStatus(null)).toBe("");
    expect(knowledgeVersionAgeConfigured(ready)).toBe(true);
    expect(knowledgeVersionAgeConfigured(null)).toBe(false);
    expect(knowledgeVersionAgeReady(ready)).toBe(true);
    expect(knowledgeVersionAgeReady({ ...ready, summary: { age_sync_status: "failed" } })).toBe(false);
    expect(knowledgeGraphTargetLabel("object_store_and_age")).toContain("AGE");
    expect(knowledgeGraphTargetLabel("object_store")).toBe("Object store");
    expect(knowledgeResolvedDefaultRetrievalMode({ graph_config: { default_retrieval_mode: "age_graph", output_target: "object_store_and_age" }, summary: { age_sync_status: "synced" } }, true)).toBe("age_graph");
    expect(knowledgeResolvedDefaultRetrievalMode({ graph_config: { default_retrieval_mode: "age_graph", output_target: "object_store" }, summary: { age_sync_status: "synced" } }, true)).toBe("graph_hybrid");
    expect(knowledgeSummaryResolvedDefaultRetrievalMode({ default_retrieval_mode: "age_graph", age_ready: true } as never, true)).toBe("age_graph");
    expect(knowledgeSummaryResolvedDefaultRetrievalMode({ default_retrieval_mode: "age_graph", age_ready: false } as never, true)).toBe("graph_hybrid");
    expect(knowledgeRetrievalModeLabel("dense", null)).toBe("Dense retrieval");
    expect(knowledgeRetrievalModeLabel("graph_hybrid", null)).toBe("GraphRAG hybrid");
    expect(knowledgeRetrievalModeLabel("age_graph", null)).toBe("Apache AGE graph");
    expect(knowledgeRetrievalModeLabel("dense", { retrieval_modes: [{ id: "dense", name: "Dense chunks" }] } as never)).toBe("Dense chunks");
    for (const state of [
      { ageReady: true, ageEnabled: true, ageConfigured: true, syncStatus: "synced", label: "AGE synced" },
      { ageReady: false, ageEnabled: false, ageConfigured: false, syncStatus: null, label: "AGE disabled" },
      { ageReady: false, ageEnabled: true, ageConfigured: false, syncStatus: null, label: "Object-store graph" },
      { ageReady: false, ageEnabled: true, ageConfigured: true, syncStatus: "failed", label: "AGE sync failed" },
      { ageReady: false, ageEnabled: true, ageConfigured: true, syncStatus: "pending", label: "AGE syncing" },
      { ageReady: false, ageEnabled: true, ageConfigured: true, syncStatus: null, label: "AGE pending" },
    ]) {
      expect(knowledgeAgeStatusLabel(state)).toBe(state.label);
    }
  });

  it("covers field metadata, constraints, formatting, and trace tags", () => {
    expect(formatFieldMetaValue(null)).toBe("—");
    expect(formatFieldMetaValue("text")).toBe("text");
    expect(formatFieldMetaValue(3)).toBe("3");
    expect(formatFieldMetaValue(true)).toBe("true");
    expect(formatFieldMetaValue({ a: 1 })).toBe('{"a":1}');
    expect(hasMeaningfulFieldMetaValue(0)).toBe(true);
    expect(hasMeaningfulFieldMetaValue("")).toBe(false);
    expect(formatJsonTextareaValue(null)).toBe("");
    expect(formatJsonTextareaValue({ a: 1 })).toContain('"a": 1');
    const field = {
      default: "default", examples: ["one", "two", "three"],
      constraints: { minimum: 1, maximum: 5, min_length: 2, max_length: 10, min_items: 1, max_items: 3, pattern: "^x", multiple_of: 2, nullable: true, options: ["a", "b", "c", "d"] },
    } as never;
    expect(fieldConstraintTokens(field)).toEqual(expect.arrayContaining(["min 1", "max 5", "min length 2", "max length 10", "min items 1", "max items 3", "pattern ^x", "step 2", "nullable", "options a, b, c…"]));
    expect(fieldMetaTokens(field)).toHaveLength(13);
    expect(formatTraceGroupTagsText({ z: "last", a: "first" })).toBe("a=first\nz=last");
    expect(parseTraceGroupTagsText(" a=1\n\nz = 2 ")).toEqual({ a: "1", z: "2" });
    expect(() => parseTraceGroupTagsText("bad")).toThrow("key=value");
    expect(() => parseTraceGroupTagsText("=bad")).toThrow("key=value");
    expect(hasAgentStructuredOutputPort(undefined)).toBe(false);
    expect(hasAgentStructuredOutputPort({ history: { type: "structured" }, tool_calls: { type: "structured" } })).toBe(false);
    expect(hasAgentStructuredOutputPort({ answer: { type: "structured" } })).toBe(true);
  });

  it("selects supported targets and normalizes binding payloads", () => {
    const manifest = {
      nodes: {
        start: { id: "start", type: "start" },
        agent: { id: "agent", type: "agent" },
        note: { id: "note", type: "note" },
      },
    } as never;
    const allowed = new Set(["agent"]) as never;
    expect(targetNodeOptions(manifest, { excludeNodeId: "start", allowedTypes: allowed, selectedNodeId: "agent" })).toEqual([{ nodeId: "agent", unsupported: false }]);
    expect(targetNodeOptions(manifest, { excludeNodeId: "start", allowedTypes: allowed, selectedNodeId: "note" })[0]).toEqual({ nodeId: "note", unsupported: true });
    expect(targetOptionLabel(manifest, { nodeId: "note", unsupported: true }, { unsupportedLabel: "Unsupported" })).toContain("Unsupported: note");
    expect(targetOptionLabel(manifest, { nodeId: "missing", unsupported: true }, { unsupportedLabel: "Unsupported" })).toBe("missing");
    expect(targetOptionLabel(manifest, { nodeId: "agent", unsupported: false }, { unsupportedLabel: "Unsupported" })).toBe("agent");
    expect(uniqueTrimmedStrings([" a ", "a", null, "", "b"])).toEqual(["a", "b"]);
    expect(workflowToolBindingTestId("tool", "My Tool")).toBe("tool-my-tool");
    expect(parseNullablePositiveNumber(" 2.5 ")).toBe(2.5);
    expect(parseNullablePositiveNumber("0")).toBeNull();
    expect(parseNullablePositiveNumber("bad")).toBeNull();
    expect(parseNonNegativeInteger("2.9")).toBe(2);
    expect(parseNonNegativeInteger("bad")).toBe(0);
    expect(parseOptionalNumber(" 3 ")).toBe(3);
    expect(parseOptionalNumber(" ")).toBeNull();
    expect(parseOptionalNumber("bad")).toBeNull();

    expect(coerceWorkflowToolBinding(null)).toBeNull();
    expect(coerceWorkflowToolBinding({ type: "mcp_tool", server_id: "s", tool_name: "t", side_effect_level: "write", requires_approval: true, max_retries: 2 })).toMatchObject({ type: "mcp_tool", side_effect_level: "write", max_retries: 2 });
    expect(coerceWorkflowToolBinding({ type: "mcp_tool", server_id: "", tool_name: "t" })).toBeNull();
    expect(coerceWorkflowToolBinding({ type: "registered_function", registry_ref: "fn", secret_refs: ["a", "a", 2], max_retries: -1 })).toMatchObject({ type: "registered_function", secret_refs: ["a"], max_retries: 0 });
    expect(coerceWorkflowToolBinding({ registry_ref: "fn" })).toMatchObject({ registry_ref: "fn", type: undefined });
    expect(coerceWorkflowToolBinding({})).toBeNull();
  });

  it("indexes bindings and deployment identifiers", () => {
    const manifest = {
      tools: {
        zed: { registry_ref: "z" },
        mcp: { type: "mcp_tool", server_id: "s", tool_name: "t" },
      },
      nodes: {
        agent: { id: "agent", type: "agent", tools: ["zed", "zed"] },
        tool: { id: "tool", type: "tool", tool_name: "mcp" },
        empty: { id: "empty", type: "tool", tool_name: " " },
      },
    } as never;
    expect(workflowToolBindingEntries(manifest)).toHaveLength(2);
    expect(workflowToolBindingReferences(manifest)).toEqual({ mcp: ["tool"], zed: ["agent"] });
    expect(workflowToolBindingTypeLabel(null)).toBe("Unsupported binding payload");
    expect(workflowToolBindingTypeLabel({ type: "mcp_tool" } as never)).toBe("MCP tool");
    expect(workflowToolBindingTypeLabel({ type: "registered_function" } as never)).toBe("Registered function");
    expect(workflowDeployGateTestId("gate", "Before Release")).toBe("gate-before-release");
  });

  it("covers artifact, trigger, validation, and subworkflow helper branches", () => {
    const manifest = {
      nodes: {
        agent: { id: "agent", type: "agent", instructions: { type: "mlflow_prompt", ref: " prompts:/support " } },
        other: { id: "other", type: "tool", instructions: { type: "inline", text: "x" } },
        empty: { id: "empty", type: "agent", instructions: { type: "mlflow_prompt", ref: " " } },
      },
      deploy_gates: { zed: { dataset_id: "z" }, alpha: { dataset_id: "a" } },
      artifacts: {
        prompts: { support: { ref: "prompts:/support@prod" } },
        eval_datasets: { eval: { dataset_id: "ds" } },
      },
    } as never;
    expect(workflowDeployGateEntries(manifest).map(([name]) => name)).toEqual(["alpha", "zed"]);
    expect(workflowArtifacts(manifest)).toHaveProperty("prompts");
    expect(workflowEvalDatasetArtifacts(manifest)).toEqual({ eval: { dataset_id: "ds" } });
    expect(workflowPromptArtifactTestId("prompt", "  Prompts:/A  ")).toBe("prompt-prompts-a");
    expect(workflowPromptArtifactTestId("prompt", " ")).toBe("prompt-prompt");
    expect(workflowPromptArtifactEntries(manifest)).toEqual([["support", { ref: "prompts:/support@prod" }]]);
    expect(workflowPromptArtifactReferences(manifest)).toEqual({ "prompts:/support": ["agent"] });
    expect(subworkflowWorkflowLabel({ workflow_id: "wf", name: "Support", status: "active" })).toBe("Support (wf)");
    expect(subworkflowWorkflowLabel({ workflow_id: "wf", name: "wf", status: "paused" })).toBe("wf · paused");
    expect(aliasChipTestId("alias", " Prod / Blue ")).toBe("alias-prod-blue");
    expect(aliasChipTestId("alias", " ")).toBe("alias-value");
    expect(subworkflowAliasTestId("prod")).toContain("inspector-subworkflow-alias");
    expect(startTriggerAliasTestId("prod")).toContain("inspector-start-alias");

    const versions = [
      { version_id: "v1", version_number: 1 },
      { version_id: "v2", version_number: 2 },
    ] as never;
    expect(subworkflowResolvedVersion(versions, "manual", null)).toMatchObject({ version_id: "v2" });
    expect(subworkflowResolvedVersion(versions, "prod", null)).toBeNull();
    expect(subworkflowResolvedVersion(versions, "prod", { version_id: "v1" } as never)).toMatchObject({ version_id: "v1" });
    expect(subworkflowResolvedVersion(versions, "prod", { version_id: "missing" } as never)).toBeNull();
    expect(workflowManifestStartTrigger(null)).toBeNull();
    expect(workflowManifestStartTrigger({ manifest: { nodes: { start: { type: "start", trigger: { mode: "cron", cron: "0 * * * *" } } } } } as never)).toEqual({ mode: "cron", cron: "0 * * * *" });
    expect(workflowManifestStartTrigger({ manifest: { nodes: { agent: { type: "agent" } } } } as never)).toBeNull();
    expect(workflowTriggerSummary(null)).toBe("Manual trigger");
    expect(workflowTriggerSummary({ mode: "event", event_name: "ready" } as never)).toContain("ready");
    expect(workflowTriggerSummary({ mode: "event" } as never)).toBe("Event trigger");
    expect(workflowTriggerSummary({ mode: "cron", cron: "0 * * * *" } as never)).toContain("Cron");
    expect(workflowTriggerSummary({ mode: "cron" } as never)).toBe("Cron trigger");
    expect(workflowValidationCounts(null)).toEqual({ errors: 0, warnings: 0, valid: true });
    expect(workflowValidationCounts({ validation_report: { errors: [{}], warnings: [{}, {}] } } as never)).toEqual({ errors: 1, warnings: 2, valid: false });
    expect(compactGraphOverrides(null)).toBeNull();
    expect(compactGraphOverrides({ retrieval_strength: "balanced", minimum_relationship_weight: 1, age_seed_mode: "entity_then_text", age_traversal_hops: 1, age_candidate_pool_size: 24, age_dense_rerank_weight: 0.3, strict_age_retrieval: true } as never)).toMatchObject({ strict_age_retrieval: true });
  });

  it("covers sparse metadata, binding, and graph-override fallbacks", () => {
    expect(normalizeWorkflowRuntimeOpenAIConfig({
      workflow_api: "",
      parallel_tool_calls: "",
      prompt_cache_mode: "",
      prompt_cache_retention: "",
    })).toBeUndefined();
    expect(knowledgeVersionAgeReady({
      graph_config: { output_target: "object_store" },
      summary: { age_sync_status: "synced" },
    } as never)).toBe(false);
    expect(knowledgeVersionAgeReady({
      graph_config: { output_target: "object_store_and_age" },
      summary: { age_sync_status: "pending" },
    } as never)).toBe(false);
    expect(knowledgeResolvedDefaultRetrievalMode(null, true)).toBe("graph_hybrid");
    expect(knowledgeSummaryResolvedDefaultRetrievalMode(null, true)).toBe("graph_hybrid");
    expect(knowledgeRetrievalModeLabel("unknown" as never, null)).toBe("Apache AGE graph");

    expect(fieldConstraintTokens({ examples: [], constraints: { options: ["a"] } } as never)).toEqual(["options a"]);
    expect(fieldConstraintTokens({ examples: [], constraints: { pattern: "" } } as never)).toEqual([]);
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(formatJsonTextareaValue(circular)).toBe("");
    expect(formatTraceGroupTagsText(null)).toBe("");
    expect(parseTraceGroupTagsText("\n a=1 \n")).toEqual({ a: "1" });
    expect(pruneToolConstraints({ a: "read", b: "", c: "write" }, ["a", "b"])).toEqual({ a: "read" });
    expect(pruneToolConstraints(undefined, [])).toEqual({});

    const manifest = { workflow_id: "wf", nodes: {
      agent: { id: "agent", type: "agent", tools: [] },
      other: { id: "other", type: "note" },
    } } as never;
    expect(targetNodeOptions(manifest, {
      excludeNodeId: "agent",
      allowedTypes: new Set(["agent"]) as never,
      selectedNodeId: "missing",
    })).toEqual([]);
    expect(workflowToolBindingReferences({ nodes: { other: { id: "other", type: "note" } } } as never)).toEqual({});
    expect(workflowPromptArtifacts({ artifacts: {} } as never)).toEqual({});
    expect(workflowPromptArtifactEntries({ artifacts: {} } as never)).toEqual([]);
    expect(workflowPromptArtifactReferences({ nodes: {
      note: { id: "note", type: "note" },
      agent: { id: "agent", type: "agent", instructions: { type: "inline", text: "x" } },
    } } as never)).toEqual({});
    expect(workflowEvalDatasetArtifacts({ artifacts: {} } as never)).toEqual({});
    expect(workflowDeployGateEntries({} as never)).toEqual([]);

    expect(workflowTriggerSummary({ mode: "event", event_name: " " } as never)).toBe("Event ·  ");
    expect(workflowTriggerSummary({ mode: "cron", cron: " " } as never)).toBe("Cron ·  ");
    expect(compactGraphOverrides({
      retrieval_strength: "",
      minimum_relationship_weight: null,
      age_seed_mode: "",
      age_traversal_hops: null,
      age_candidate_pool_size: null,
      age_dense_rerank_weight: null,
      strict_age_retrieval: false,
    } as never)).toBeNull();
  });
});
