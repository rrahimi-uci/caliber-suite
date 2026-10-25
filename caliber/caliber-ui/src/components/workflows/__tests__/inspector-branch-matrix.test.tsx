import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Inspector } from "@/components/workflows/Inspector";

const manifestFor = (node: Record<string, unknown>) => ({
  schema_version: 1,
  workflow_id: "wf-1",
  name: "Coverage workflow",
  nodes: { [String(node.id)]: node },
  edges: [],
}) as never;

const renderInspector = (
  node: Record<string, unknown>,
  options: Record<string, unknown> = {},
) => {
  const onChangeNode = vi.fn();
  const onChangeWorkflow = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <Inspector
        manifest={manifestFor(node)}
        selectedNodeId={String(node.id)}
        tools={[]}
        {...(options as never)}
        onChangeNode={onChangeNode}
        onChangeWorkflow={onChangeWorkflow}
      />
    </QueryClientProvider>,
  );
  return { ...view, onChangeNode, onChangeWorkflow };
};

describe("Inspector branch matrix", () => {
  it("covers agent prompt modes, tool constraints, structured output, and evaluation controls", async () => {
    const user = userEvent.setup();
    const agent = {
      id: "agent",
      type: "agent",
      name: "Support agent",
      model: "inherit",
      instructions: { type: "inline", text: "Answer with evidence." },
      tools: ["registered.tool"],
      tool_constraints: { "registered.tool": "required_before_claim" },
      skills: [],
      outputs: { final_output: { type: "string" } },
    };
    const options = {
      tools: [
        {
          value: "registered.tool",
          label: "registered.tool",
          hint: "Registered lookup",
        },
      ],
      prompts: [
        {
          agent_id: "support",
          prompt_name: "support",
          has_prompt: true,
          alias: "prod",
        } as never,
      ],
      skills: [{ name: "grounding", summary: "Ground answers" } as never],
      evalDatasets: [{ dataset_id: "ds-1", name: "Support evals" } as never],
    };

    const first = renderInspector(agent, options);
    fireEvent.change(screen.getByTestId("inspector-instructions"), {
      target: { value: "Updated inline instructions" },
    });
    expect(first.onChangeNode).toHaveBeenCalledWith("agent", {
      instructions: { type: "inline", text: "Updated inline instructions" },
    });
    await user.click(screen.getByTestId("instructions-mode-prompt"));
    expect(first.onChangeNode).toHaveBeenCalledWith("agent", {
      instructions: { type: "mlflow_prompt", ref: "" },
    });
    await user.selectOptions(
      screen.getByTestId("inspector-tool-constraint-registered_tool"),
      "",
    );
    expect(first.onChangeNode).toHaveBeenCalledWith("agent", {
      tool_constraints: {},
    });
    first.unmount();

    const promptAgent = renderInspector(
      {
        ...agent,
        instructions: { type: "mlflow_prompt", ref: "support" },
      },
      options,
    );
    await user.selectOptions(
      screen.getByTestId("inspector-prompt-ref"),
      "support",
    );
    await user.click(screen.getByTestId("instructions-mode-inline"));
    expect(promptAgent.onChangeNode).toHaveBeenCalledWith("agent", {
      instructions: { type: "inline", text: "" },
    });

    fireEvent.change(screen.getByTestId("inspector-agent-output-type"), {
      target: { value: "[]" },
    });
    fireEvent.blur(screen.getByTestId("inspector-agent-output-type"));
    expect(await screen.findByText("Schema must be a JSON object.")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("inspector-agent-output-type"), {
      target: { value: '{"type":"object","properties":{"answer":{"type":"string"}}}' },
    });
    fireEvent.blur(screen.getByTestId("inspector-agent-output-type"));
    await user.selectOptions(screen.getByTestId("inspector-agent-eval-dataset"), "ds-1");
    expect(promptAgent.onChangeNode).toHaveBeenCalledWith("agent", {
      eval_dataset: "ds-1",
    });
  });

  it("covers workflow-level runtime, artifact, binding, and deploy-gate controls", async () => {
    const user = userEvent.setup();
    const onChangeWorkflow = vi.fn();
    const manifest = {
      schema_version: 1,
      workflow_id: "wf-settings",
      name: "Settings workflow",
      owner: "@ops",
      description: "A workflow with governed runtime settings.",
      runtime: {
        session: { type: "persistent" },
        default_model_ref: "MODEL_REF",
        sdk: "custom-sdk",
        sdk_version_policy: "pinned",
        compiler_version: "compiler-v2",
        openai: {
          workflow_api: "responses",
          parallel_tool_calls: "enabled",
          prompt_cache_mode: "auto",
          prompt_cache_retention: "24h",
        },
      },
      mlflow: {
        experiment_name: "ops",
        trace_group_tags: { team: "ops" },
      },
      artifacts: {
        prompts: {
          support: { registry_name: "support", alias: "prod", managed_by: "registry" },
        },
        eval_datasets: { ds1: { dataset_name: "Support set" } },
      },
      deploy_gates: {
        release: {
          type: "deploy_gate",
          dataset_ref: "ds1",
          required_for_aliases: ["prod"],
          thresholds: { min_pass_rate: 0.8 },
        },
      },
      tools: {
        registered: {
          type: "registered_function",
          registry_ref: "fn.lookup",
          version_constraint: ">=1",
          timeout_seconds: 10,
          max_retries: 1,
          requires_approval: false,
          secret_refs: ["SECRET"],
        },
        mcp: {
          type: "mcp_tool",
          server_id: "mcp-1",
          tool_name: "search",
          schema_version: "1",
          side_effect_level: "read",
          timeout_seconds: 20,
          max_retries: 0,
          requires_approval: false,
        },
      },
      nodes: {
        agent: { id: "agent", type: "agent", tools: ["registered"] },
        direct: { id: "direct", type: "tool", tool_name: "mcp" },
      },
      edges: [],
    } as never;

    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Inspector
          manifest={manifest}
          selectedNodeId={null}
          tools={[]}
          prompts={[
            {
              agent_id: "support",
              prompt_name: "support",
              has_prompt: true,
              alias: "prod",
            } as never,
          ]}
          evalDatasets={[{ dataset_id: "ds1", name: "Support set" } as never]}
          mcpServers={[]}
          onChangeNode={vi.fn()}
          onChangeWorkflow={onChangeWorkflow}
        />
      </QueryClientProvider>,
    );

    await user.selectOptions(screen.getByTestId("workflow-session-mode"), "in_memory");
    await user.selectOptions(screen.getByTestId("workflow-openai-api"), "chat_completions");
    await user.selectOptions(screen.getByTestId("workflow-openai-parallel-tool-calls"), "disabled");
    await user.selectOptions(screen.getByTestId("workflow-openai-prompt-cache-mode"), "disabled");
    await user.selectOptions(screen.getByTestId("workflow-openai-prompt-cache-retention"), "default");
    fireEvent.change(screen.getByTestId("workflow-mlflow-experiment-name"), {
      target: { value: "new-exp" },
    });
    fireEvent.change(screen.getByTestId("workflow-mlflow-trace-group-tags"), {
      target: { value: "bad line" },
    });
    fireEvent.blur(screen.getByTestId("workflow-mlflow-trace-group-tags"));
    expect(
      await screen.findByText(/Each trace tag line must use key=value format/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("workflow-mlflow-trace-group-tags"), {
      target: { value: "team=platform\nservice=runtime" },
    });
    fireEvent.blur(screen.getByTestId("workflow-mlflow-trace-group-tags"));

    await user.click(screen.getByTestId("workflow-prompt-artifacts-add"));
    await user.click(screen.getByTestId("workflow-deploy-gates-add"));
    expect(screen.getByText("Deploy gate")).toBeInTheDocument();
    const gateCard = screen.getByTestId("workflow-deploy-gate-card-release");
    await user.selectOptions(
      within(gateCard).getByTestId("workflow-deploy-gate-dataset-release"),
      "ds1",
    );
    await user.click(
      within(gateCard).getByTestId("workflow-deploy-gate-alias-prod-release"),
    );
    fireEvent.change(
      within(gateCard).getByTestId("workflow-deploy-gate-threshold-min-pass-rate-release"),
      { target: { value: "" } },
    );
    await user.click(
      within(screen.getByTestId("workflow-tool-binding-card-registered")).getByRole(
        "button",
        { name: "Remove" },
      ),
    );
    expect(onChangeWorkflow).toHaveBeenCalled();
  });

  it("covers empty governance panels and every editable binding branch", async () => {
    const user = userEvent.setup();
    const emptyWorkflow = {
      schema_version: 1,
      workflow_id: "wf-empty-settings",
      name: "Empty settings workflow",
      nodes: {},
      edges: [],
    } as never;
    const emptyChange = vi.fn();
    const emptyView = render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Inspector
          manifest={emptyWorkflow}
          selectedNodeId={null}
          tools={[]}
          prompts={[]}
          evalDatasets={[]}
          onChangeNode={vi.fn()}
          onChangeWorkflow={emptyChange}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText(/No prompt registry records are loaded right now/)).toBeInTheDocument();
    expect(screen.getByText(/Select one or more tools on agent nodes/)).toBeInTheDocument();
    expect(screen.getByText("No deploy gates configured yet.")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-deploy-gates-add")).toBeDisabled();
    await user.click(screen.getByTestId("workflow-prompt-artifacts-add"));
    expect(emptyChange).toHaveBeenCalledWith(
      expect.objectContaining({
        artifacts: expect.objectContaining({
          prompts: expect.objectContaining({
            prompt_artifact_1: expect.objectContaining({ managed_by: "manual" }),
          }),
        }),
      }),
    );
    emptyView.unmount();

    const onChangeWorkflow = vi.fn();
    const bindingWorkflow = {
      schema_version: 1,
      workflow_id: "wf-binding-matrix",
      name: "Binding matrix workflow",
      nodes: {},
      edges: [],
      tools: {
        legacy: { unexpected: true },
        registered: {
          type: "registered_function",
          registry_ref: "fn.lookup",
          version_constraint: ">=1",
          timeout_seconds: 10,
          max_retries: 1,
          requires_approval: false,
          secret_refs: ["OLD_SECRET"],
        },
        mcp: {
          type: "mcp_tool",
          server_id: "mcp-old",
          tool_name: "search-old",
          tool_schema_version: "1",
          side_effect_level: "read",
          timeout_seconds: 20,
          max_retries: 0,
          requires_approval: false,
        },
      },
      deploy_gates: {
        legacy_gate: {
          type: "deploy_gate",
          dataset_ref: "missing-dataset",
          required_for_aliases: [],
          thresholds: {},
        },
      },
    } as never;

    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Inspector
          manifest={bindingWorkflow}
          selectedNodeId={null}
          tools={[]}
          prompts={[]}
          evalDatasets={[]}
          onChangeNode={vi.fn()}
          onChangeWorkflow={onChangeWorkflow}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText("The current payload is outside the supported workflow binding schema.")).toBeInTheDocument();
    expect(screen.getByText("missing-dataset (not currently active)")).toBeInTheDocument();

    const registered = screen.getByTestId("workflow-tool-binding-card-registered");
    fireEvent.change(
      within(registered).getByTestId("workflow-tool-binding-registry-ref-registered"),
      { target: { value: "fn.updated" } },
    );
    fireEvent.change(
      within(registered).getByTestId("workflow-tool-binding-version-constraint-registered"),
      { target: { value: "^2" } },
    );
    fireEvent.change(
      within(registered).getByTestId("workflow-tool-binding-timeout-registered"),
      { target: { value: "" } },
    );
    fireEvent.change(
      within(registered).getByTestId("workflow-tool-binding-retries-registered"),
      { target: { value: "3" } },
    );
    await user.click(within(registered).getByTestId("workflow-tool-binding-approval-registered"));
    fireEvent.change(
      within(registered).getByTestId("workflow-tool-binding-secret-refs-registered"),
      { target: { value: "SECRET_A\nSECRET_B\nSECRET_A" } },
    );

    const mcp = screen.getByTestId("workflow-tool-binding-card-mcp");
    fireEvent.change(
      within(mcp).getByTestId("workflow-tool-binding-server-id-mcp"),
      { target: { value: "mcp-new" } },
    );
    fireEvent.change(
      within(mcp).getByTestId("workflow-tool-binding-tool-name-mcp"),
      { target: { value: "search-new" } },
    );
    fireEvent.change(
      within(mcp).getByTestId("workflow-tool-binding-schema-version-mcp"),
      { target: { value: "2" } },
    );
    await user.selectOptions(
      within(mcp).getByTestId("workflow-tool-binding-side-effect-mcp"),
      "external_action",
    );
    fireEvent.change(
      within(mcp).getByTestId("workflow-tool-binding-timeout-mcp"),
      { target: { value: "" } },
    );
    fireEvent.change(
      within(mcp).getByTestId("workflow-tool-binding-retries-mcp"),
      { target: { value: "2" } },
    );
    await user.click(within(mcp).getByTestId("workflow-tool-binding-approval-mcp"));

    const gate = screen.getByTestId("workflow-deploy-gate-card-legacy-gate");
    expect(within(gate).getByText("Pick the dataset CALIBER should evaluate before promoting this alias.")).toBeInTheDocument();
    await user.click(within(gate).getByTestId("workflow-deploy-gate-alias-prod-legacy-gate"));
    fireEvent.change(
      within(gate).getByTestId("workflow-deploy-gate-aliases-legacy-gate"),
      { target: { value: "prod, staging, prod" } },
    );
    for (const [testId, value] of [
      ["workflow-deploy-gate-threshold-min-pass-rate-legacy-gate", "0.9"],
      ["workflow-deploy-gate-threshold-min-overall-delta-legacy-gate", "0.02"],
      ["workflow-deploy-gate-threshold-max-p95-latency-ms-legacy-gate", "250"],
    ] as const) {
      fireEvent.change(within(gate).getByTestId(testId), { target: { value } });
    }
    await user.click(within(gate).getByTestId("workflow-deploy-gate-remove-legacy-gate"));
    await user.click(within(mcp).getByTestId("workflow-tool-binding-remove-mcp"));
    await user.click(within(registered).getByTestId("workflow-tool-binding-remove-registered"));
    await user.click(within(screen.getByTestId("workflow-tool-binding-card-legacy")).getByTestId("workflow-tool-binding-remove-legacy"));
    expect(onChangeWorkflow).toHaveBeenCalled();
  });

  it("covers webhook defaults, methods, and editable headers", async () => {
    const user = userEvent.setup();
    const { onChangeNode, unmount } = renderInspector({ id: "hook", type: "webhook" });

    fireEvent.change(screen.getByTestId("inspector-webhook-url"), {
      target: { value: "https://example.test/hook" },
    });
    await user.selectOptions(screen.getByTestId("inspector-webhook-method"), "PATCH");
    fireEvent.change(screen.getByTestId("inspector-webhook-timeout"), {
      target: { value: "45" },
    });
    expect(screen.getByText("No headers configured.")).toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-api-add-header"));
    expect(onChangeNode).toHaveBeenCalledWith("hook", { headers: { "": "" } });
    expect(onChangeNode).toHaveBeenCalledWith("hook", {
      url: "https://example.test/hook",
    });
    expect(onChangeNode).toHaveBeenCalledWith("hook", { method: "PATCH" });
    expect(onChangeNode).toHaveBeenCalledWith("hook", { timeout_seconds: 45 });
    unmount();

    const existing = renderInspector({
      id: "hook-2",
      type: "webhook",
      headers: { Accept: "application/json" },
      method: "GET",
      timeout_seconds: 10,
    });
    expect(screen.getByLabelText("Header 1 name")).toHaveValue("Accept");
    fireEvent.change(screen.getByLabelText("Header 1 name"), {
      target: { value: "X-Trace" },
    });
    fireEvent.change(screen.getByLabelText("Header 1 value"), {
      target: { value: "trace-1" },
    });
    await user.click(screen.getByRole("button", { name: "Remove header 1" }));
    expect(existing.onChangeNode).toHaveBeenCalledWith("hook-2", { headers: {} });
  });

  it("covers API request URL/cURL branches and safe header editing", async () => {
    const user = userEvent.setup();
    const { onChangeNode, unmount } = renderInspector({
      id: "api",
      type: "api_request",
      mode: "url",
      url: "",
      method: "GET",
      headers: {},
    });
    await user.selectOptions(screen.getByTestId("inspector-api-method"), "POST");
    fireEvent.change(screen.getByTestId("inspector-api-body"), {
      target: { value: '{"ok":true}' },
    });
    await user.click(screen.getByTestId("inspector-api-add-header"));
    expect(onChangeNode).toHaveBeenCalledWith("api", { headers: { "": "" } });
    await user.click(screen.getByTestId("inspector-api-mode-curl"));
    expect(onChangeNode).toHaveBeenCalledWith("api", { mode: "curl" });
    unmount();

    const curl = renderInspector({
      id: "api-curl",
      type: "api_request",
      mode: "curl",
      curl: "",
      headers: {},
    });
    fireEvent.change(screen.getByTestId("inspector-api-curl"), {
      target: { value: "curl https://example.test" },
    });
    expect(curl.onChangeNode).toHaveBeenCalledWith("api-curl", {
      curl: "curl https://example.test",
    });
    expect(onChangeNode).toHaveBeenCalledWith("api", {
      method: "POST",
    });
    curl.unmount();

    renderInspector({
      id: "api-custom",
      type: "api_request",
      mode: "url",
      url: "https://example.test",
      headers: { Accept: "application/json" },
      timeout_seconds: 30,
    });
    expect(screen.getByTestId("inspector-api-connector-preset")).toHaveValue("custom");
    expect(screen.getByLabelText("Header 1 value")).toHaveValue("application/json");
  });

  it("covers every data-transform operation, JSON validation, and review queue fields", async () => {
    const user = userEvent.setup();
    const { onChangeNode } = renderInspector({
      id: "transform",
      type: "data_transform",
      operation: "mapping",
      config: { fields: { id: "input.id" } },
      fail_on_invalid: true,
    });
    const operation = screen.getByTestId("inspector-transform-operation");
    for (const value of ["fixture", "mapping", "json_schema", "decision_table", "confidence"]) {
      await user.selectOptions(operation, value);
    }
    const config = screen.getByTestId("inspector-transform-config");
    fireEvent.change(config, { target: { value: "[]" } });
    fireEvent.blur(config);
    expect(await screen.findByText("Configuration must be a JSON object.")).toBeInTheDocument();
    fireEvent.change(config, { target: { value: '{"valid":true}' } });
    fireEvent.blur(config);
    expect(onChangeNode).toHaveBeenCalledWith("transform", {
      config: { valid: true },
    });
    await user.click(screen.getByTestId("inspector-transform-fail-invalid"));
    expect(onChangeNode).toHaveBeenCalledWith("transform", { fail_on_invalid: false });

    const review = renderInspector({
      id: "review",
      type: "review_queue_enqueue",
      queue_id: "RQ-1",
      experiment_id: "exp-1",
      assigned_to: "@reviewer",
    });
    fireEvent.change(screen.getByTestId("inspector-review-queue-id"), {
      target: { value: "RQ-2" },
    });
    fireEvent.change(screen.getByTestId("inspector-review-experiment-id"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByTestId("inspector-review-assigned-to"), {
      target: { value: "" },
    });
    expect(review.onChangeNode).toHaveBeenCalledWith("review", { queue_id: "RQ-2" });
    expect(review.onChangeNode).toHaveBeenCalledWith("review", { experiment_id: null });
    expect(review.onChangeNode).toHaveBeenCalledWith("review", { assigned_to: null });
  });

  it("covers template, external-app, guardrail, and router configuration branches", async () => {
    const user = userEvent.setup();
    const template = renderInspector({
      id: "template-rich",
      type: "template",
      template: "Hello {{input}}",
      output_format: "text",
      missing_variable_mode: "preserve",
    });
    fireEvent.change(screen.getByTestId("inspector-template-body"), {
      target: { value: "JSON {{variables.customer.id}}" },
    });
    await user.selectOptions(screen.getByTestId("inspector-template-output-format"), "json");
    await user.selectOptions(screen.getByTestId("inspector-template-missing-mode"), "error");
    expect(template.onChangeNode).toHaveBeenCalledWith("template-rich", {
      output_format: "json",
    });
    template.unmount();

    const external = renderInspector({
      id: "external-rich",
      type: "external_app",
      entrypoint: "support.ticketing:handle",
    });
    fireEvent.change(screen.getByTestId("inspector-external-entrypoint"), {
      target: { value: "billing.refunds:open_case" },
    });
    expect(external.onChangeNode).toHaveBeenCalledWith("external-rich", {
      entrypoint: "billing.refunds:open_case",
    });
    external.unmount();

    const guardrail = renderInspector({
      id: "guardrail-rich",
      type: "guardrail",
      mode: "pre_agent",
      on_failure: "block",
      checks: [
        { kind: "pii_detection", params: { entities: ["email"] } },
        { non_empty_output: {} },
      ],
    });
    await user.selectOptions(screen.getByTestId("inspector-on-failure"), "block_retry");
    fireEvent.change(screen.getByTestId("inspector-guardrail-max-retries"), {
      target: { value: "3" },
    });
    await user.click(screen.getByTestId("check-0-entity-ssn"));
    await user.selectOptions(screen.getByTestId("check-add-kind"), "max_length");
    await user.click(screen.getByTestId("check-add"));
    await user.click(screen.getByTestId("check-remove-1"));
    expect(guardrail.onChangeNode).toHaveBeenCalledWith("guardrail-rich", {
      max_retries: 3,
    });
    guardrail.unmount();

    const router = renderInspector(
      {
        id: "router-rich",
        type: "router",
        branches: [
          { condition: { field: "status", op: "equals", value: "ready" }, to: "agent" },
          { condition: null, to: "fallback" },
        ],
      },
      {},
    );
    fireEvent.change(screen.getByTestId("router-field-0"), {
      target: { value: "result.status" },
    });
    await user.selectOptions(screen.getByTestId("router-op-0"), "not_equals");
    fireEvent.change(screen.getByTestId("router-value-0"), {
      target: { value: "failed" },
    });
    await user.selectOptions(screen.getByTestId("router-else"), "router-rich");
    await user.click(screen.getByTestId("router-add-condition"));
    expect(router.onChangeNode).toHaveBeenCalled();
  });

  it("renders the editor panel for every non-knowledge node family", () => {
    const types = [
      "start",
      "output",
      "agent",
      "guardrail",
      "file_input",
      "folder_input",
      "input_bucket",
      "output_bucket",
      "output_folder",
      "wait_until",
      "wait_for_event",
      "parallel",
      "join",
      "for_each",
      "loop",
      "error_boundary",
      "tool",
      "mcp_resource",
      "template",
      "external_app",
      "python_code",
      "router",
      "human_approval",
      "note",
    ];

    for (const type of types) {
      const { unmount } = renderInspector({ id: `node-${type}`, type });
      expect(screen.getByTestId("wf-inspector")).toHaveAttribute(
        "data-node-type",
        type,
      );
      unmount();
    }
  });

  it("exercises every editable inspector control across the node families", () => {
    const fixtures: Array<Record<string, unknown>> = [
      {
        id: "agent-edit",
        type: "agent",
        instructions: { type: "inline", text: "Answer precisely." },
        output_type: "text",
        tools: [],
      },
      { id: "file-edit", type: "file_input", path: "docs/a.txt", max_bytes: 10 },
      {
        id: "folder-edit",
        type: "folder_input",
        path: "docs",
        pattern: "**/*.md",
        recursive: false,
        max_files: 5,
      },
      { id: "input-bucket-edit", type: "input_bucket", bucket: "in", prefix: "docs", recursive: false, encoding: "utf-8" },
      { id: "output-bucket-edit", type: "output_bucket", bucket: "out", prefix: "runs", overwrite: false },
      { id: "output-folder-edit", type: "output_folder", path: "out", overwrite: false },
      { id: "wait-until-edit", type: "wait_until", wait_until: "2026-01-01T00:00:00Z", timezone: "UTC" },
      { id: "wait-event-edit", type: "wait_for_event", event_name: "ticket.updated", timeout_seconds: 60 },
      { id: "foreach-edit", type: "for_each", target: "items", item_variable: "item" },
      { id: "loop-edit", type: "loop", target: "items", max_iterations: 3, stop_condition: "done" },
      { id: "boundary-edit", type: "error_boundary", target: "agent", compensate: true },
      { id: "tool-edit", type: "tool", tool_name: "lookup", timeout_seconds: 20 },
      { id: "mcp-edit", type: "mcp_resource", server: "server", resource: "resource", timeout_seconds: 20 },
      { id: "python-edit", type: "python_code", code: "return 1", timeout_seconds: 20 },
      { id: "router-edit", type: "router", mode: "first_match", on_failure: "stop" },
      { id: "guardrail-edit", type: "guardrail", mode: "all", on_failure: "stop", max_retries: 2 },
      { id: "approval-edit", type: "human_approval", role: "reviewer", required_approvals: 1, timeout_seconds: 60 },
      { id: "note-edit", type: "note", text: "Operator note" },
    ];

    for (const fixture of fixtures) {
      const { onChangeNode, unmount } = renderInspector(fixture);
      let changed = 0;
      for (const control of screen.queryAllByRole("combobox")) {
        if (control instanceof HTMLSelectElement && control.disabled) continue;
        const option = control.querySelector("option:not([disabled])") as HTMLOptionElement | null;
        if (option) {
          fireEvent.change(control, { target: { value: option.value } });
          changed += 1;
        }
      }
      for (const control of screen.queryAllByRole("textbox")) {
        if ((control as HTMLInputElement).disabled) continue;
        fireEvent.change(control, { target: { value: "updated-value" } });
        changed += 1;
      }
      for (const control of screen.queryAllByRole("spinbutton")) {
        if ((control as HTMLInputElement).disabled) continue;
        fireEvent.change(control, { target: { value: "7" } });
        changed += 1;
      }
      for (const control of screen.queryAllByRole("checkbox")) {
        if ((control as HTMLInputElement).disabled) continue;
        fireEvent.click(control);
        changed += 1;
      }
      if (changed > 0) {
        expect(onChangeNode).toHaveBeenCalled();
      }
      unmount();
    }
  });

  it("covers event and cron trigger editing, alias guidance, and preview states", async () => {
    const user = userEvent.setup();
    const eventView = renderInspector({
      id: "start-event",
      type: "start",
      trigger: { mode: "event", event_name: "order.created", alias: "prod", enabled: true },
    });

    expect(screen.getByTestId("inspector-start-event-name")).toHaveValue("order.created");
    await user.click(screen.getByTestId("inspector-start-alias-prod"));
    await user.selectOptions(screen.getByLabelText("Start mode"), "manual");
    expect(eventView.onChangeNode).toHaveBeenCalledWith("start-event", { trigger: null });
    eventView.unmount();

    const cronView = renderInspector({
      id: "start-cron",
      type: "start",
      trigger: { mode: "cron", cron: "0 9 * * *", timezone: "UTC", alias: "unknown", enabled: false },
    });
    expect(screen.getByTestId("inspector-start-cron-preview")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-start-alias-unknown")).toBeInTheDocument();
    expect(screen.getByText(/Loading deployment aliases|No active deployments exist|Deployment aliases could not be loaded/)).toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-start-alias-prod"));
    await user.click(screen.getByRole("checkbox", { name: "Enabled" }));
    expect(cronView.onChangeNode).toHaveBeenCalledWith("start-cron", {
      trigger: { mode: "cron", cron: "0 9 * * *", timezone: "UTC", alias: "prod", enabled: false },
    });
    expect(cronView.onChangeNode).toHaveBeenCalledWith("start-cron", {
      trigger: { mode: "cron", cron: "0 9 * * *", timezone: "UTC", alias: "unknown", enabled: true },
    });
  });

  it("renders component schema guidance, validation feedback, advanced fields, and output actions", async () => {
    const user = userEvent.setup();
    const onDeleteNode = vi.fn();
    const view = renderInspector(
      {
        id: "agent-schema",
        type: "agent",
        name: "",
        instructions: { type: "inline", text: "Answer" },
        tools: [],
      },
      {
        componentSpec: {
          id: "agent-component",
          type: "agent",
          name: "Agent component",
          category: "AI",
          description: "Schema guidance",
          docs: ["Use the configured agent runtime."],
          default_inputs: {},
          default_outputs: {},
          requirements: [],
          setup_checks: [],
          fields: [
            { key: "name", label: "Name", type: "string", required: true, examples: [] },
            { key: "model", label: "Model", type: "string", advanced: true, examples: [] },
          ],
        },
        validationReport: {
          errors: [{ path: "nodes.agent-schema.name", message: "Name is required", severity: "error" }],
          warnings: [],
        },
        lastStep: {
          node_id: "agent-schema",
          node_type: "agent",
          status: "ok",
          output: "answer",
          detail: "completed",
          duration_ms: 20,
          tool_calls: [{ name: "lookup" }],
        },
        onDeleteNode,
        focusFieldKey: "name",
        focusFieldSignal: 1,
      },
    );

    expect(screen.getAllByText("Name is required").length).toBeGreaterThan(0);
    expect(screen.getByTestId("inspector-node-output")).toHaveTextContent("answer");
    expect(screen.getByTestId("inspector-node-output")).toHaveTextContent("1 tool call");
    expect(screen.queryByTestId("inspector-field-model")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-toggle-advanced"));
    expect(screen.getByTestId("inspector-field-model")).toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-delete"));
    expect(onDeleteNode).toHaveBeenCalledWith("agent-schema");
    view.unmount();
  });
});
