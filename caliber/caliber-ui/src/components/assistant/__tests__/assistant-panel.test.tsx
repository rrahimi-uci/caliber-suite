/**
 * Tests for the CaliberAssistantPanel (MLflow-style drawer).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { CaliberAssistantPanel } from "@/components/assistant/CaliberAssistantPanel";
import {
  AssistantPanelProvider,
  useAssistantPanel,
} from "@/components/assistant/AssistantPanelContext";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const ACTIVE_SESSION_KEY = "caliber.assistant.session.active";
const PANEL_WIDTH_KEY = "caliber.assistant.panel.width";

function envelope<T>(data: T): { data: T } {
  return { data };
}

/**
 * Helper that renders the panel already opened, plus an external toggle
 * button for close tests.
 */
function renderPanel(): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });

  function Inner(): JSX.Element {
    const { open, toggle } = useAssistantPanel();
    return (
      <>
        {!open && (
          <button type="button" onClick={toggle}>
            Open Assistant
          </button>
        )}
        <CaliberAssistantPanel />
      </>
    );
  }

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <AssistantPanelProvider>
          <Inner />
        </AssistantPanelProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openPanel(): Promise<void> {
  const btn = screen.getByText("Open Assistant");
  await userEvent.click(btn);
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("CaliberAssistantPanel", () => {
  it("is hidden until opened", () => {
    renderPanel();
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    expect(screen.getByText("Open Assistant")).toBeInTheDocument();
  });

  it("renders header with title when open", async () => {
    renderPanel();
    await openPanel();
    expect(screen.getByText("Aria")).toBeInTheDocument();
  });

  it("restores a persisted active session and hydrates its runtime metadata", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-restore0001");
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-restore0001",
              title: "Restored session",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: {
                assistant_skill_runtime: {
                  mode: "manual",
                  pinned_skill_names: ["doc-search"],
                  disabled_skill_names: [],
                  last_selected_skills: [],
                },
              },
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(
          envelope([
            {
              message_id: "AMSG-restore0001",
              session_id: "ASST-restore0001",
              role: "assistant",
              content: "Restored answer",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    renderPanel();
    await openPanel();

    expect(await screen.findByText("Restored answer")).toBeInTheDocument();
    expect(window.localStorage.getItem(ACTIVE_SESSION_KEY)).toBe(
      "ASST-restore0001",
    );
  });

  it("renders assistant process steps and action traces from message metadata", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-process0001");
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-process0001",
              title: "Process session",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: {},
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(
          envelope([
            {
              message_id: "AMSG-process0001",
              session_id: "ASST-process0001",
              role: "assistant",
              content: "I prepared a draft and it now needs your review.",
              metadata_: {
                process_steps: [
                  { key: "thinking", label: "Thinking", tone: "neutral" },
                  { key: "review", label: "Review required", tone: "warning" },
                ],
                tool_calls: [
                  {
                    name: "preview_workflow_draft",
                    arguments: {},
                    result_summary: "draft prepared",
                    ok: true,
                  },
                ],
              },
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    renderPanel();
    await openPanel();

    expect(await screen.findByText("Review required")).toBeInTheDocument();
    expect(screen.getByTestId("assistant-tool-calls")).toHaveTextContent(
      "Actions · 1",
    );
  });

  it("clears stale persisted session ids after the session list loads", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-missing");

    renderPanel();
    await openPanel();

    await waitFor(() => {
      expect(window.localStorage.getItem(ACTIVE_SESSION_KEY)).toBeNull();
    });
    expect(
      screen.getByText(
        "Create a tool that validates email addresses and returns clear error messages",
      ),
    ).toBeInTheDocument();
  });

  it("supports collapsing, expanding, and resizing the desktop panel", async () => {
    renderPanel();
    await openPanel();

    await userEvent.click(screen.getByLabelText("Collapse assistant"));
    expect(screen.getByLabelText("Expand assistant")).toBeInTheDocument();
    expect(screen.queryByLabelText("New Chat")).not.toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("Expand assistant"));
    expect(screen.getByText("Aria")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByTestId("assistant-resize-handle"), {
      clientX: 500,
    });
    await waitFor(() => {
      expect(document.body.style.cursor).toBe("col-resize");
    });
    fireEvent.mouseMove(window, { clientX: 200 });

    await waitFor(() => {
      expect(window.localStorage.getItem(PANEL_WIDTH_KEY)).toBe("680");
    });

    fireEvent.mouseUp(window);
    await waitFor(() => {
      expect(document.body.style.cursor).toBe("");
    });
  });

  it("does not expose assistant settings inside the chat panel", async () => {
    renderPanel();
    await openPanel();
    expect(screen.queryByLabelText("Model settings")).not.toBeInTheDocument();
    expect(screen.queryByText("Assistant Settings")).not.toBeInTheDocument();
  });

  it("shows suggested prompts in empty state", async () => {
    renderPanel();
    await openPanel();
    expect(
      screen.getByText(
        "Create a tool that validates email addresses and returns clear error messages",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Build a skill that summarizes support tickets with severity and next action",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Add an MCP server and wire it into a workflow for live data access",
      ),
    ).toBeInTheDocument();
  });

  it("shows welcome text in empty state", async () => {
    renderPanel();
    await openPanel();
    expect(
      screen.getByText(/i can help you design and create tools/i),
    ).toBeInTheDocument();
  });

  it("closes when close button is clicked", async () => {
    renderPanel();
    await openPanel();
    expect(screen.getByTestId("assistant-panel")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("Close"));
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
  });

  it("closes when Escape is pressed", async () => {
    renderPanel();
    await openPanel();
    expect(screen.getByTestId("assistant-panel")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    });
  });

  it("creates session and shows chat input when New Chat is clicked", async () => {
    renderPanel();
    await openPanel();

    await userEvent.click(screen.getByLabelText("New Chat"));

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Ask Aria for follow-up changes..."),
      ).toBeInTheDocument();
    });
  });

  it("sends a message and shows assistant response", async () => {
    let posted = false;
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        async ({ request }) => {
          posted = true;
          postedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              assistant_message: {
                message_id: "AMSG-resp0001",
                session_id: "ASST-00000001",
                role: "assistant",
                content: "I'll help you create a tool.",
                metadata_: {},
                sequence_number: 1,
                created_at: new Date().toISOString(),
              },
              questions: [],
              draft_updates: [],
              run: null,
            }),
            { status: 201 },
          );
        },
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () => {
        if (!posted) return HttpResponse.json(envelope([]));
        return HttpResponse.json(
          envelope([
            {
              message_id: "AMSG-user0001",
              session_id: "ASST-00000001",
              role: "user",
              content: "Create a greeting tool",
              metadata_: {},
              sequence_number: 0,
              created_at: new Date().toISOString(),
            },
            {
              message_id: "AMSG-resp0001",
              session_id: "ASST-00000001",
              role: "assistant",
              content: "I'll help you create a tool.",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
          ]),
        );
      }),
    );

    renderPanel();
    await openPanel();

    // Create session first
    await userEvent.click(screen.getByLabelText("New Chat"));

    const input = await screen.findByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "Create a greeting tool");
    await userEvent.click(screen.getByLabelText("Send message"));

    await waitFor(() => {
      expect(
        screen.getByText(/I'll help you create a tool/),
      ).toBeInTheDocument();
    });
    expect(postedBody?.current_surface).toBe("assistant_drawer");
    expect(
      screen.getAllByTestId("assistant-message-avatar").length,
    ).toBeGreaterThan(0);
  });

  it("shows clarifying questions from assistant", async () => {
    server.use(
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-question0001",
              session_id: "ASST-msw0001",
              role: "assistant",
              content: "I'll help you create a tool.",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
            questions: [
              {
                question: "What should the tool be named?",
                field: "name",
                options: [],
              },
            ],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        ),
      ),
    );

    renderPanel();
    await openPanel();

    // Create session first
    await userEvent.click(screen.getByLabelText("New Chat"));

    const input = await screen.findByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "Create a tool");
    await userEvent.click(screen.getByLabelText("Send message"));

    await waitFor(() => {
      expect(
        screen.getByText("What should the tool be named?"),
      ).toBeInTheDocument();
    });
  });

  it("creates session from suggested prompt click", async () => {
    renderPanel();
    await openPanel();

    await userEvent.click(
      screen.getByText(
        "Create a tool that validates email addresses and returns clear error messages",
      ),
    );

    // After auto-creating session and sending message, input should appear
    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Ask Aria for follow-up changes..."),
      ).toBeInTheDocument();
    });
  });

  it("sends a suggested prompt without a decorative emoji prefix", async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        async ({ request }) => {
          postedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              assistant_message: {
                message_id: "AMSG-sp01",
                session_id: "ASST-00000001",
                role: "assistant",
                content: "ok",
                metadata_: {},
                sequence_number: 1,
                created_at: new Date().toISOString(),
              },
              questions: [],
              draft_updates: [],
              run: null,
            }),
            { status: 201 },
          );
        },
      ),
    );
    renderPanel();
    await openPanel();

    await userEvent.click(
      screen.getByText(
        "Create a tool that validates email addresses and returns clear error messages",
      ),
    );

    await waitFor(() => expect(postedBody).not.toBeNull());
    const content = (postedBody as Record<string, unknown>).content as string;
    expect(content).toBe(
      "Create a tool that validates email addresses and returns clear error messages",
    );
    // No emoji should leak into the message content sent to the model.
    expect(/\p{Extended_Pictographic}/u.test(content)).toBe(false);
  });

  it("shows New Chat button in header", async () => {
    renderPanel();
    await openPanel();
    expect(screen.getByLabelText("New Chat")).toBeInTheDocument();
  });

  it("has send button disabled when input is empty", async () => {
    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Ask Aria for follow-up changes..."),
      ).toBeInTheDocument();
    });

    expect(screen.getByLabelText("Send message")).toBeDisabled();
  });

  it("shows drafts toggle when drafts exist", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () => {
        return HttpResponse.json(
          envelope([
            {
              draft_id: "ADRF-00000001",
              session_id: "ASST-00000001",
              artifact_type: "tool",
              status: "draft",
              title: "greeting_tool",
              summary: "A tool that greets users.",
              spec: {},
              artifact: { name: "greeting_tool" },
              validation_report: null,
              test_report: null,
              target_registry_id: null,
              version: 1,
              created_by: "@test",
              updated_by: "@test",
              created_at: "2025-06-01T00:00:00Z",
              updated_at: "2025-06-01T00:01:00Z",
            },
          ]),
        );
      }),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    // After session creation, drafts should load
    await waitFor(() => {
      expect(screen.getByText(/1 draft generated/)).toBeInTheDocument();
    });
  });

  it("shows draft cards when drafts toggle is clicked", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () => {
        return HttpResponse.json(
          envelope([
            {
              draft_id: "ADRF-00000001",
              session_id: "ASST-00000001",
              artifact_type: "tool",
              status: "draft",
              title: "greeting_tool",
              summary: "A tool that greets users.",
              spec: {},
              artifact: { name: "greeting_tool" },
              validation_report: null,
              test_report: null,
              target_registry_id: null,
              version: 1,
              created_by: "@test",
              updated_by: "@test",
              created_at: "2025-06-01T00:00:00Z",
              updated_at: "2025-06-01T00:01:00Z",
            },
          ]),
        );
      }),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    // Wait for drafts to load and click the inline link
    const draftLink = await screen.findByText(/1 draft generated/);
    await userEvent.click(draftLink);

    expect(await screen.findByText("greeting_tool")).toBeInTheDocument();
    // "Drafts (1)" appears in both the toggle button sr-only text and the
    // drafts header. Use getAllByText to confirm both are present.
    const draftHeaders = screen.getAllByText("Drafts (1)");
    expect(draftHeaders.length).toBeGreaterThanOrEqual(1);
  });

  it("renders close assistant button for mobile", async () => {
    renderPanel();
    await openPanel();
    expect(screen.getByLabelText("Close assistant")).toBeInTheDocument();
  });

  it("has the updated follow-up placeholder", async () => {
    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    expect(
      await screen.findByPlaceholderText("Ask Aria for follow-up changes..."),
    ).toBeInTheDocument();
  });

  it("runs draft lifecycle actions from the drafts drawer", async () => {
    const makeDraft = (id: string, status: string, title: string) => ({
      draft_id: id,
      session_id: "ASST-msw0001",
      artifact_type: "tool",
      status,
      title,
      summary: `${title} summary`,
      spec: {},
      artifact: { name: title, runtime: "python" },
      validation_report: null,
      test_report: null,
      target_registry_id: null,
      version: 1,
      created_by: "@test",
      updated_by: "@test",
      created_at: "2025-06-01T00:00:00Z",
      updated_at: "2025-06-01T00:01:00Z",
    });
    let validateCalls = 0;
    let testCalls = 0;
    let approveCalls = 0;
    let publishCalls = 0;
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(
          envelope([
            makeDraft("ADRF-draft", "draft", "draft_tool"),
            makeDraft("ADRF-validated", "validated", "validated_tool"),
            makeDraft("ADRF-tested", "tested", "tested_tool"),
            makeDraft("ADRF-approved", "approved", "approved_tool"),
          ]),
        ),
      ),
      http.post(`${API_BASE}/assistant/drafts/ADRF-draft/validate`, () => {
        validateCalls += 1;
        return HttpResponse.json(
          envelope(makeDraft("ADRF-draft", "validated", "draft_tool")),
        );
      }),
      http.post(`${API_BASE}/assistant/drafts/ADRF-validated/test`, () => {
        testCalls += 1;
        return HttpResponse.json(
          envelope(makeDraft("ADRF-validated", "tested", "validated_tool")),
        );
      }),
      http.post(`${API_BASE}/assistant/drafts/ADRF-tested/approve`, () => {
        approveCalls += 1;
        return HttpResponse.json(
          envelope(makeDraft("ADRF-tested", "approved", "tested_tool")),
        );
      }),
      http.post(`${API_BASE}/assistant/drafts/ADRF-approved/publish`, () => {
        publishCalls += 1;
        return HttpResponse.json(
          envelope(makeDraft("ADRF-approved", "published", "approved_tool")),
        );
      }),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));
    await userEvent.click(await screen.findByText(/4 drafts generated/));

    await userEvent.click(
      screen.getAllByRole("button", { name: "Details" })[0],
    );
    expect(await screen.findByText(/"runtime": "python"/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));

    await userEvent.click(screen.getByRole("button", { name: "Validate" }));
    await userEvent.click(screen.getByRole("button", { name: "Test" }));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => {
      expect(validateCalls).toBe(1);
      expect(testCalls).toBe(1);
      expect(approveCalls).toBe(1);
      expect(publishCalls).toBe(1);
    });
  });

  it("auto-creates session when typing a message from empty state", async () => {
    let sessionCreated = false;
    let messagePosted = false;
    server.use(
      http.post(`${API_BASE}/assistant/sessions`, () => {
        sessionCreated = true;
        return HttpResponse.json(
          envelope({
            session_id: "ASST-auto0001",
            title: "New session",
            goal: "",
            artifact_type: null,
            status: "active",
            created_by: "@test",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
          { status: 201 },
        );
      }),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, () => {
        messagePosted = true;
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-auto0002",
              session_id: "ASST-auto0001",
              role: "assistant",
              content: "Sure, I can help with that.",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () => {
        if (!messagePosted) return HttpResponse.json(envelope([]));
        return HttpResponse.json(
          envelope([
            {
              message_id: "AMSG-auto0001",
              session_id: "ASST-auto0001",
              role: "user",
              content: "Hello there",
              metadata_: {},
              sequence_number: 0,
              created_at: new Date().toISOString(),
            },
            {
              message_id: "AMSG-auto0002",
              session_id: "ASST-auto0001",
              role: "assistant",
              content: "Sure, I can help with that.",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
          ]),
        );
      }),
    );

    renderPanel();
    await openPanel();

    // Type directly in the input — no "New Chat" click needed
    const input = screen.getByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "Hello there");
    await userEvent.click(screen.getByLabelText("Send message"));

    await waitFor(() => {
      expect(sessionCreated).toBe(true);
      expect(messagePosted).toBe(true);
    });

    await waitFor(() => {
      expect(
        screen.getByText("Sure, I can help with that."),
      ).toBeInTheDocument();
    });
  });
});
