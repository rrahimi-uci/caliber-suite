import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type { ToolDefinition } from "@/api/workflowTypes";
import { ToolPlayground, ToolTests } from "@/pages/ToolRegistry";

const NOW = "2026-01-01T00:00:00Z";

function makeTool(overrides: Partial<ToolDefinition> = {}): ToolDefinition {
  return {
    tool_id: "TL-1",
    name: "lookup_order",
    version: "1.0",
    description: "Lookup an order by id",
    module_path: "caliber.workflows.demo_tools",
    callable_name: "lookup_order",
    input_schema: {
      type: "object",
      properties: { order_id: { type: "string", description: "Order id" } },
      required: ["order_id"],
    },
    output_schema: {
      type: "object",
      properties: { status: { type: "string" } },
    },
    side_effect_level: "read",
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: [],
    test_cases: [],
    last_calibration: null,
    owner: "@team",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ToolPlayground panel", () => {
  it("renders empty/loading states", () => {
    const { rerender } = render(<ToolPlayground tools={[]} loading />);
    expect(screen.getByText("Loading tools…")).toBeInTheDocument();

    rerender(<ToolPlayground tools={[]} loading={false} />);
    expect(screen.getByText("No tools registered yet.")).toBeInTheDocument();
  });

  it("runs tests, shows result, and manages history", async () => {
    vi.spyOn(caliberApi, "testRunTool").mockResolvedValue({
      tool_id: "TL-1",
      output: { status: "shipped" },
      mocked: false,
      duration_ms: 17,
      error: null,
    });

    const user = userEvent.setup();
    render(<ToolPlayground tools={[makeTool()]} loading={false} />);
    await screen.findByText("Tool Details");

    fireEvent.change(screen.getByPlaceholderText('{"key": "value"}'), {
      target: { value: '{"order_id":"123"}' },
    });
    await user.click(screen.getByRole("button", { name: /Test Run/i }));

    expect(await screen.findByText("Success")).toBeInTheDocument();
    expect(screen.getByText("History")).toBeInTheDocument();
    const entries = screen.getAllByText("lookup_order");
    await user.click(entries[entries.length - 1]!);
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByText("History")).not.toBeInTheDocument();
  });

  it("shows error result when test run request fails", async () => {
    vi.spyOn(caliberApi, "testRunTool").mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    render(<ToolPlayground tools={[makeTool()]} loading={false} />);
    await screen.findByText("Tool Details");

    await user.click(screen.getByRole("button", { name: /Test Run/i }));
    expect(await screen.findByText("Error")).toBeInTheDocument();
    expect(screen.getByText("Request failed")).toBeInTheDocument();
  });
});

describe("ToolTests panel", () => {
  it("generates and runs unit tests with judge scoring", async () => {
    vi.spyOn(caliberApi, "getAssistantConfig").mockResolvedValue({
      engine: "fake",
      model: "gpt-4o-mini",
      provider: "openai",
      reasoning: "medium",
      enabled: true,
      disabled_intents: [],
      disabled_domains: [],
      available_models: [{ id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" }],
    });
    vi.spyOn(caliberApi, "updateAssistantConfig").mockImplementation(async (payload) => ({
      engine: "fake",
      model: payload.model ?? "gpt-4o-mini",
      provider: "openai",
      reasoning: "medium",
      enabled: true,
      disabled_intents: [],
      disabled_domains: [],
      available_models: [{ id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" }],
    }));

    let sessionCount = 0;
    vi.spyOn(caliberApi, "createAssistantSession").mockImplementation(async () => {
      sessionCount += 1;
      return {
        session_id: `ASST-${sessionCount}`,
        title: "session",
        owner: "@test",
        status: "active",
        goal: "",
        metadata_: null,
        active_draft_id: null,
        created_at: NOW,
        updated_at: NOW,
      };
    });
    vi.spyOn(caliberApi, "sendAssistantMessage").mockImplementation(async (_sessionId, body) => {
      if (body.content.includes("Generate the unit test cases now")) {
        return {
          assistant_message: {
            message_id: "m-gen",
            session_id: "ASST-1",
            role: "assistant",
            content: JSON.stringify([
              {
                input: { order_id: "1" },
                expectedOutput: { status: "open" },
                expectedBehavior: "returns order status",
                tags: ["happy"],
              },
              {
                input: { order_id: "2" },
                expectedOutput: { status: "closed" },
                expectedBehavior: "returns closed",
                tags: ["state"],
              },
            ]),
            metadata_: {},
            sequence_number: 1,
            created_at: NOW,
          },
          questions: [],
          draft_updates: [],
          run: null,
        };
      }
      return {
        assistant_message: {
          message_id: "m-judge",
          session_id: "ASST-x",
          role: "assistant",
          content: '{"verdict":"pass","score":0.9,"reasoning":"Matches expectation."}',
          metadata_: {},
          sequence_number: 1,
          created_at: NOW,
        },
        questions: [],
        draft_updates: [],
        run: null,
      };
    });
    vi.spyOn(caliberApi, "testRunTool").mockImplementation(async (_toolId, input) => ({
      tool_id: "TL-1",
      output: { status: (input.order_id as string) === "1" ? "open" : "closed" },
      mocked: false,
      duration_ms: 5,
      error: null,
    }));

    const user = userEvent.setup();
    render(<ToolTests tools={[makeTool()]} loading={false} />);
    expect(await screen.findByText("No generated unit tests yet.")).toBeInTheDocument();

    await user.click(screen.getByTestId("tool-tests-generate"));
    expect(await screen.findByText("Unit Test 1")).toBeInTheDocument();
    expect(screen.getByText("Unit Test 2")).toBeInTheDocument();

    await user.click(screen.getByTestId("tool-tests-run"));
    await waitFor(() => {
      expect(screen.getAllByText(/pass/i).length).toBeGreaterThan(0);
    });
    expect(screen.getByText(/Average score 90%/)).toBeInTheDocument();
  });

  it("handles invalid generation output and run-time tool failures", async () => {
    vi.spyOn(caliberApi, "getAssistantConfig").mockResolvedValue({
      engine: "fake",
      model: "gpt-4o-mini",
      provider: "openai",
      reasoning: "medium",
      enabled: true,
      disabled_intents: [],
      disabled_domains: [],
      available_models: [{ id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" }],
    });
    vi.spyOn(caliberApi, "createAssistantSession").mockResolvedValue({
      session_id: "ASST-1",
      title: "session",
      owner: "@test",
      status: "active",
      goal: "",
      metadata_: null,
      active_draft_id: null,
      created_at: NOW,
      updated_at: NOW,
    });
    const sendSpy = vi.spyOn(caliberApi, "sendAssistantMessage");
    sendSpy.mockResolvedValueOnce({
      assistant_message: {
        message_id: "m-gen",
        session_id: "ASST-1",
        role: "assistant",
        content: "not json array",
        metadata_: {},
        sequence_number: 1,
        created_at: NOW,
      },
      questions: [],
      draft_updates: [],
      run: null,
    });
    sendSpy.mockResolvedValueOnce({
      assistant_message: {
        message_id: "m-gen2",
        session_id: "ASST-1",
        role: "assistant",
        content: JSON.stringify([
          {
            input: { order_id: "1" },
            expectedOutput: { status: "open" },
            expectedBehavior: "returns order status",
            tags: [],
          },
        ]),
        metadata_: {},
        sequence_number: 2,
        created_at: NOW,
      },
      questions: [],
      draft_updates: [],
      run: null,
    });
    sendSpy.mockRejectedValueOnce(new Error("Judge failed"));

    vi.spyOn(caliberApi, "testRunTool").mockRejectedValue(new Error("runtime down"));

    const user = userEvent.setup();
    render(<ToolTests tools={[makeTool()]} loading={false} />);

    await user.click(screen.getByTestId("tool-tests-generate"));
    expect(await screen.findByText(/JSON array/)).toBeInTheDocument();

    await user.click(screen.getByTestId("tool-tests-generate"));
    expect(await screen.findByText("Unit Test 1")).toBeInTheDocument();
    await user.click(screen.getByTestId("tool-tests-run"));

    await waitFor(() => {
      expect(screen.getAllByText(/runtime down/).length).toBeGreaterThan(0);
    });
  });

  it("shows loading and empty states", () => {
    const { rerender } = render(<ToolTests tools={[]} loading />);
    expect(screen.getByText("Loading tools…")).toBeInTheDocument();
    rerender(<ToolTests tools={[]} loading={false} />);
    expect(screen.getByText("No tools registered yet.")).toBeInTheDocument();
  });
});
