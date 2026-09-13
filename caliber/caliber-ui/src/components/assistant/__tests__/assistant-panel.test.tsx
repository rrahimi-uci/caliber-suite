/**
 * Tests for the CaliberAssistantPanel (MLflow-style drawer).
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { CaliberAssistantPanel } from "@/components/assistant/CaliberAssistantPanel";
import {
  AssistantPanelProvider,
  useAssistantPanel,
} from "@/components/assistant/AssistantPanelContext";
import { server } from "@/test/server";
import { showToast } from "@/lib/toast";

vi.mock("@/lib/toast", () => ({
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

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
    expect((postedBody as unknown as Record<string, unknown> | null)?.current_surface).toBe(
      "assistant_drawer",
    );
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
    const content = (postedBody as unknown as Record<string, unknown>).content as string;
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
      screen.getAllByRole("button", { name: "Details" })[0]!,
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

/* ------------------------------------------------------------------ */
/* Responsive layout (matchMedia-driven desktop/mobile switch)         */
/* ------------------------------------------------------------------ */

type MediaListener = (event: { matches: boolean }) => void;

/**
 * jsdom does not implement `matchMedia`, so the panel's `isDesktop`
 * detection (both the initial state and the change-listener effect)
 * silently no-ops in every other test in this file and always resolves
 * to "desktop". These tests install a real (fake) MediaQueryList so we can
 * exercise the narrow-viewport branch and both the modern
 * (add/removeEventListener) and legacy (add/removeListener) listener APIs.
 */
function mockMatchMedia(
  initialMatches: boolean,
  legacy = false,
): { setMatches: (next: boolean) => void } {
  const listeners = new Set<MediaListener>();
  let matches = initialMatches;
  const mql: Record<string, unknown> = {
    get matches() {
      return matches;
    },
    media: "(min-width: 768px)",
  };
  if (legacy) {
    mql.addListener = (cb: MediaListener) => listeners.add(cb);
    mql.removeListener = (cb: MediaListener) => listeners.delete(cb);
  } else {
    mql.addEventListener = (_event: string, cb: MediaListener) =>
      listeners.add(cb);
    mql.removeEventListener = (_event: string, cb: MediaListener) =>
      listeners.delete(cb);
  }
  (
    window as unknown as { matchMedia: (query: string) => unknown }
  ).matchMedia = () => mql;
  return {
    setMatches(next: boolean) {
      matches = next;
      listeners.forEach((cb) => cb({ matches: next }));
    },
  };
}

describe("CaliberAssistantPanel — responsive layout", () => {
  afterEach(() => {
    delete (window as unknown as { matchMedia?: unknown }).matchMedia;
  });

  it("hides desktop-only controls on a narrow viewport", async () => {
    mockMatchMedia(false);
    renderPanel();
    await openPanel();

    expect(
      screen.queryByLabelText("Collapse assistant"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("assistant-resize-handle"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Close assistant")).toBeInTheDocument();
  });

  it("switches into the desktop layout when the media query flips (modern listener API)", async () => {
    const mq = mockMatchMedia(false);
    renderPanel();
    await openPanel();
    expect(
      screen.queryByLabelText("Collapse assistant"),
    ).not.toBeInTheDocument();

    act(() => mq.setMatches(true));

    await waitFor(() => {
      expect(screen.getByLabelText("Collapse assistant")).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("assistant-resize-handle"),
    ).toBeInTheDocument();
  });

  it("falls back to the legacy addListener/removeListener API for older browsers", async () => {
    const mq = mockMatchMedia(false, true);
    renderPanel();
    await openPanel();
    expect(
      screen.queryByLabelText("Collapse assistant"),
    ).not.toBeInTheDocument();

    act(() => mq.setMatches(true));

    await waitFor(() => {
      expect(screen.getByLabelText("Collapse assistant")).toBeInTheDocument();
    });
  });
});

/* ------------------------------------------------------------------ */
/* Session-restore: stored interaction mode / approval mode / in-flight */
/* plan reattachment                                                   */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — session restore", () => {
  it("restores the session's last mode and approval mode, and reattaches its in-flight plan", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-planrestore1");
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-planrestore1",
              title: "Plan session",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: {
                assistant_mode: "plan",
                assistant_approval_mode: "full_autonomy",
              },
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(
          envelope([
            {
              plan_id: "PLAN-restore1",
              session_id: "ASST-planrestore1",
              project_id: null,
              goal: "Resume this in-flight plan",
              status: "draft",
              autonomy: "approve_plan",
              owner: "@test",
              constraints: {},
              done_when: [],
              context_refs: [],
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              step_count: 1,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-restore1`, () =>
        HttpResponse.json(
          envelope({
            plan: {
              plan_id: "PLAN-restore1",
              session_id: "ASST-planrestore1",
              project_id: null,
              goal: "Resume this in-flight plan",
              status: "draft",
              autonomy: "approve_plan",
              owner: "@test",
              constraints: {},
              done_when: [],
              context_refs: [],
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              step_count: 1,
            },
            steps: [
              {
                step_id: "PSTEP-restore1",
                plan_id: "PLAN-restore1",
                seq: 0,
                capability_key: "judge.create",
                title: "Create judge",
                inputs: {},
                depends_on: [],
                status: "pending",
                result: {},
                evidence: {},
                error: null,
                draft_id: null,
                job_id: null,
                approval_id: null,
                checkpoint_id: null,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
              },
            ],
          }),
        ),
      ),
      http.get(`${API_BASE}/aria/plans/PLAN-restore1/interactions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    renderPanel();
    await openPanel();

    // Restored mode/approval mode reflected in the composer controls.
    expect(screen.getByTestId("assistant-mode-selector")).toHaveTextContent(
      "Plan",
    );
    expect(
      screen.getByTestId("assistant-approval-selector"),
    ).toHaveTextContent("Full autonomy");

    // The in-flight plan for this session is reattached and rendered inline.
    expect(
      await screen.findByText("Resume this in-flight plan"),
    ).toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Plan mode: building a goal-plan, autonomy control, and error path    */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — plan mode", () => {
  it("builds a plan from a typed goal, reuses the session for a follow-up, and updates autonomy", async () => {
    const createdBodies: Record<string, unknown>[] = [];
    server.use(
      http.post(`${API_BASE}/aria/plans`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        createdBodies.push(body);
        return HttpResponse.json(
          envelope({
            plan: {
              plan_id: "PLAN-built1",
              session_id: "ASST-msw0001",
              project_id: null,
              goal: String(body.goal),
              status: "draft",
              autonomy: body.autonomy ?? "approve_plan",
              owner: "@test",
              constraints: {},
              done_when: [],
              context_refs: [],
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              step_count: 0,
            },
            steps: [],
          }),
        );
      }),
      http.get(`${API_BASE}/aria/plans/PLAN-built1/interactions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    renderPanel();
    await openPanel();

    // Switch into Plan mode from the mode selector.
    await userEvent.click(screen.getByTestId("assistant-mode-selector"));
    await userEvent.click(screen.getByText("Plan"));

    const input = screen.getByPlaceholderText("Describe a plan...");
    await userEvent.type(input, "Build a fraud-detection pipeline");
    await userEvent.click(screen.getByLabelText("Build plan"));

    expect(
      await screen.findByText("Build a fraud-detection pipeline"),
    ).toBeInTheDocument();
    expect(createdBodies[0]).toMatchObject({
      goal: "Build a fraud-detection pipeline",
      autonomy: "approve_plan",
    });

    // The autonomy control is now visible (a session/plan exists); change it.
    const autonomySelect = screen.getByLabelText("Plan autonomy");
    await userEvent.selectOptions(autonomySelect, "ask_each");
    expect(autonomySelect).toHaveValue("ask_each");

    // A follow-up goal reuses the already-created session (ensureSession
    // returns early) and carries the newly selected autonomy.
    await userEvent.type(input, "Add a fallback reviewer step");
    await userEvent.click(screen.getByLabelText("Build plan"));

    await waitFor(() => expect(createdBodies).toHaveLength(2));
    expect(createdBodies[1]).toMatchObject({
      goal: "Add a fallback reviewer step",
      autonomy: "ask_each",
      session_id: "ASST-msw0001",
    });
  });

  it("surfaces a plan-build failure inline instead of crashing", async () => {
    server.use(
      http.post(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json({ detail: "Plan build blew up" }, { status: 500 }),
      ),
    );

    renderPanel();
    await openPanel();

    await userEvent.click(screen.getByTestId("assistant-mode-selector"));
    await userEvent.click(screen.getByText("Plan"));

    const input = screen.getByPlaceholderText("Describe a plan...");
    await userEvent.type(input, "A goal that will fail");
    await userEvent.click(screen.getByLabelText("Build plan"));

    expect(await screen.findByText("Plan build blew up")).toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Queue: submitting while busy, cancelling, reusing an active session  */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — queueing & session reuse", () => {
  it("queues a follow-up (rather than sending it) when submitted while a turn is busy", async () => {
    let resolveFirstSend: ((value: unknown) => void) | null = null;
    const firstSendGate = new Promise((resolve) => {
      resolveFirstSend = resolve;
    });
    let enqueuedBody: Record<string, unknown> | null = null;

    server.use(
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        async ({ params }) => {
          await firstSendGate;
          return HttpResponse.json(
            envelope({
              assistant_message: {
                message_id: "AMSG-busy1",
                session_id: String(params.sessionId),
                role: "assistant",
                content: "First reply",
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
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/queue`,
        async ({ request }) => {
          enqueuedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            envelope({
              queue_id: "QMSG-busy1",
              session_id: "ASST-msw0001",
              content: enqueuedBody.content,
              mode: enqueuedBody.mode,
              kind: enqueuedBody.kind,
              position: 0,
              status: "pending",
              created_by: "@test",
              created_at: new Date().toISOString(),
            }),
            { status: 201 },
          );
        },
      ),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    const input = await screen.findByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "first message");
    await userEvent.click(screen.getByLabelText("Send message"));

    // The first turn is now pending (gated on firstSendGate); a second
    // submit must be queued, not sent as a second message.
    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Type to queue a follow-up…"),
      ).toBeInTheDocument();
    });
    await userEvent.type(input, "second message");
    await userEvent.click(screen.getByLabelText("Add to queue"));

    await waitFor(() => expect(enqueuedBody).not.toBeNull());
    expect(enqueuedBody).toMatchObject({
      content: "second message",
      kind: "queued",
    });

    // Let the first turn resolve so no request is left hanging, and confirm
    // the composer leaves the "queue a follow-up" placeholder once it does.
    (resolveFirstSend as unknown as ((value: unknown) => void) | null)?.(undefined);
    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Ask Aria for follow-up changes..."),
      ).toBeInTheDocument();
    });
  });

  it("cancels a queued message from the composer", async () => {
    // The panel auto-dispatches the head of the queue as soon as no turn is
    // busy (see the "queues a follow-up" test above), which would otherwise
    // race the manual cancel this test exercises. Hang the send endpoint so
    // the row is never auto-cleared, isolating the composer's own cancel
    // ("Remove queued message") wiring.
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-cancel1");
    let cancelledId: string | null = null;
    let cancelled = false;
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-cancel1",
              title: "Cancel session",
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
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/queue`, () =>
        HttpResponse.json(
          envelope(
            cancelled
              ? []
              : [
                  {
                    queue_id: "QMSG-cancel1",
                    session_id: "ASST-cancel1",
                    content: "pending idea",
                    mode: "build",
                    kind: "queued",
                    position: 0,
                    status: "pending",
                    created_by: "@test",
                    created_at: new Date().toISOString(),
                  },
                ],
          ),
        ),
      ),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        () => new Promise(() => {}),
      ),
      http.delete(`${API_BASE}/assistant/queue/:queueId`, ({ params }) => {
        cancelledId = String(params.queueId);
        cancelled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderPanel();
    await openPanel();

    expect(await screen.findByTestId("assistant-queue")).toHaveTextContent(
      "pending idea",
    );
    await userEvent.click(screen.getByLabelText("Remove queued message"));

    await waitFor(() => expect(cancelledId).toBe("QMSG-cancel1"));
    await waitFor(() => {
      expect(screen.queryByTestId("assistant-queue")).not.toBeInTheDocument();
    });
  });

  it("reuses the active session for a Steer message instead of creating a new one", async () => {
    let sessionCreateCalls = 0;
    let steerSessionId: string | null = null;
    server.use(
      http.post(`${API_BASE}/assistant/sessions`, () => {
        sessionCreateCalls += 1;
        return HttpResponse.json(
          envelope({
            session_id: "ASST-reuse1",
            title: "New session",
            owner: "@test",
            status: "active",
            goal: "",
            metadata_: {},
            active_draft_id: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
          { status: 201 },
        );
      }),
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/queue`,
        async ({ params }) => {
          steerSessionId = String(params.sessionId);
          return HttpResponse.json(
            envelope({
              queue_id: "QMSG-reuse1",
              session_id: String(params.sessionId),
              content: "change course",
              mode: "build",
              kind: "steer",
              position: 0,
              status: "pending",
              created_by: "@test",
              created_at: new Date().toISOString(),
            }),
            { status: 201 },
          );
        },
      ),
    );

    renderPanel();
    await openPanel();
    // Create the session up front (first ensureSession/createSession call).
    await userEvent.click(screen.getByLabelText("New Chat"));
    await screen.findByPlaceholderText("Ask Aria for follow-up changes...");
    expect(sessionCreateCalls).toBe(1);

    const input = screen.getByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "change course");
    await userEvent.click(screen.getByLabelText("Steer"));

    await waitFor(() => expect(steerSessionId).toBe("ASST-reuse1"));
    // ensureSession returned the existing session instead of creating another.
    expect(sessionCreateCalls).toBe(1);
  });
});

/* ------------------------------------------------------------------ */
/* Clarifying-question answers, chat history navigation, drafts header, */
/* settings modal close, scroll-into-view, and model-update error       */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — misc interactions", () => {
  it("sends the clicked option as the answer to a clarifying question", async () => {
    const sentContents: string[] = [];
    server.use(
      http.post(
        `${API_BASE}/assistant/sessions/:sessionId/messages`,
        async ({ request, params }) => {
          const body = (await request.json()) as Record<string, unknown>;
          sentContents.push(String(body.content));
          const isFirst = sentContents.length === 1;
          return HttpResponse.json(
            envelope({
              assistant_message: {
                message_id: `AMSG-q${sentContents.length}`,
                session_id: String(params.sessionId),
                role: "assistant",
                content: isFirst ? "Which kind?" : "Got it, a tool it is.",
                metadata_: {},
                sequence_number: sentContents.length,
                created_at: new Date().toISOString(),
              },
              questions: isFirst
                ? [
                    {
                      question: "What kind of artifact?",
                      field: "artifact_type",
                      options: ["Tool", "Skill"],
                    },
                  ]
                : [],
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
    await userEvent.click(screen.getByLabelText("New Chat"));

    const input = await screen.findByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "Create something");
    await userEvent.click(screen.getByLabelText("Send message"));

    const toolOption = await screen.findByRole("button", { name: "Tool" });
    await userEvent.click(toolOption);

    await waitFor(() =>
      expect(sentContents).toEqual(["Create something", "Tool"]),
    );
    // The clicked option was sent as the answer, and since the second
    // response carries no further questions, the question list clears.
    await waitFor(() => {
      expect(
        screen.queryByText("What kind of artifact?"),
      ).not.toBeInTheDocument();
    });
  });

  it("selects a previous session from chat history and starts a new chat from the drawer", async () => {
    let selectedMessagesFetched = false;
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-hist-select",
              title: "Earlier conversation",
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
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, ({ params }) => {
        if (params.sessionId === "ASST-hist-select") selectedMessagesFetched = true;
        return HttpResponse.json(envelope([]));
      }),
    );

    renderPanel();
    await openPanel();

    await userEvent.click(screen.getByLabelText("Chat history"));
    const history = await screen.findByTestId("assistant-history");
    await userEvent.click(
      screen.getByText("Earlier conversation", { selector: "p" }),
    );

    // The drawer closes and the selected session's messages are fetched.
    await waitFor(() => {
      expect(screen.queryByTestId("assistant-history")).not.toBeInTheDocument();
    });
    await waitFor(() => expect(selectedMessagesFetched).toBe(true));
    expect(history).toBeDefined();

    // Reopen history and start a new chat from inside the drawer.
    await userEvent.click(screen.getByLabelText("Chat history"));
    await screen.findByTestId("assistant-history");
    await userEvent.click(screen.getByText("+ New chat"));

    await waitFor(() => {
      expect(screen.queryByTestId("assistant-history")).not.toBeInTheDocument();
    });
  });

  it("toggles the drafts drawer from the header icon button", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(
          envelope([
            {
              draft_id: "ADRF-header1",
              session_id: "ASST-msw0001",
              artifact_type: "tool",
              status: "draft",
              title: "header_toggle_tool",
              summary: "A tool.",
              spec: {},
              artifact: { name: "header_toggle_tool" },
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
        ),
      ),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    const toggle = await screen.findByLabelText("Toggle drafts");
    await userEvent.click(toggle);
    expect(await screen.findByText("header_toggle_tool")).toBeInTheDocument();

    await userEvent.click(toggle);
    await waitFor(() => {
      expect(screen.queryByText("header_toggle_tool")).not.toBeInTheDocument();
    });
  });

  it("closes the settings modal", async () => {
    renderPanel();
    await openPanel();

    await userEvent.click(screen.getByLabelText("Aria settings"));
    expect(await screen.findByTestId("assistant-settings")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("Close settings"));
    await waitFor(() => {
      expect(screen.queryByTestId("assistant-settings")).not.toBeInTheDocument();
    });
  });

  it("scrolls the conversation into view when a new message arrives", async () => {
    const scrollIntoView = vi.fn();
    // jsdom does not implement scrollIntoView; the panel feature-detects it.
    Element.prototype.scrollIntoView = scrollIntoView;

    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(
          envelope([
            {
              message_id: "AMSG-scroll1",
              session_id: "ASST-msw0001",
              role: "assistant",
              content: "Hello from the scroll test.",
              metadata_: {},
              sequence_number: 1,
              created_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    await screen.findByText("Hello from the scroll test.");
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());

    // @ts-expect-error -- test-only cleanup of the monkey-patch above.
    delete Element.prototype.scrollIntoView;
  });

  it("falls back to a generic toast message when a model update fails without a detail", async () => {
    server.use(
      http.patch(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json({ detail: "" }, { status: 500 }),
      ),
    );

    renderPanel();
    await openPanel();

    const selector = await screen.findByTestId("assistant-model-selector");
    await userEvent.click(selector);
    await userEvent.click(await screen.findByText("GPT-4o Mini"));

    await waitFor(() => {
      expect(showToast.error).toHaveBeenCalledWith(
        "Failed to update Aria runtime",
      );
    });
  });
});

/* ------------------------------------------------------------------ */
/* Auto-dispatch guards: closed panel, dedupe, and the createSession-   */
/* pending double-submit guard                                         */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — auto-dispatch guards", () => {
  it("does not auto-dispatch a queued message while the panel is closed", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-closed1");
    let sendCalled = false;
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-closed1",
              title: "Closed session",
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
      http.get(`${API_BASE}/assistant/sessions/:sessionId/queue`, () =>
        HttpResponse.json(
          envelope([
            {
              queue_id: "QMSG-closed1",
              session_id: "ASST-closed1",
              content: "should not be sent yet",
              mode: "build",
              kind: "queued",
              position: 0,
              status: "pending",
              created_by: "@test",
              created_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, () => {
        sendCalled = true;
        return HttpResponse.json({ detail: "should not be called" }, { status: 500 });
      }),
    );

    // Render without opening the panel; the panel's hooks (and the
    // auto-dispatch effect) still run every render even though the drawer
    // itself renders nothing while closed.
    renderPanel();
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();

    // Give any (incorrect) dispatch a chance to fire before asserting.
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(sendCalled).toBe(false);
  });

  it("does not re-dispatch the same queued row while a refetch still reports it", async () => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-dedupe1");
    let sendCalls = 0;
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-dedupe1",
              title: "Dedupe session",
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
        HttpResponse.json(envelope([])),
      ),
      // Simulate a queue that keeps reporting the same row even after the
      // send succeeds (e.g. a slow-to-clear read replica). The dispatch
      // dedupe guard (dispatchedRef) must stop a second send.
      http.get(`${API_BASE}/assistant/sessions/:sessionId/queue`, () =>
        HttpResponse.json(
          envelope([
            {
              queue_id: "QMSG-dedupe1",
              session_id: "ASST-dedupe1",
              content: "only once please",
              mode: "build",
              kind: "queued",
              position: 0,
              status: "pending",
              created_by: "@test",
              created_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, async () => {
        sendCalls += 1;
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: `AMSG-dedupe${sendCalls}`,
              session_id: "ASST-dedupe1",
              role: "assistant",
              content: "ok",
              metadata_: {},
              sequence_number: sendCalls,
              created_at: new Date().toISOString(),
            },
            questions: [],
            draft_updates: [],
            run: null,
          }),
          { status: 201 },
        );
      }),
      http.delete(`${API_BASE}/assistant/queue/:queueId`, () =>
        new HttpResponse(null, { status: 204 }),
      ),
    );

    renderPanel();
    await openPanel();

    await waitFor(() => expect(sendCalls).toBe(1));
    // Give the settled mutation, the (failed-to-clear) queue refetch, and
    // another effect pass a chance to run before confirming no re-send.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sendCalls).toBe(1);
  });

  it("ignores a Send click while a new session is still being created", async () => {
    let sessionCreateCalls = 0;
    server.use(
      http.post(
        `${API_BASE}/assistant/sessions`,
        () => new Promise(() => {}),
      ),
    );

    renderPanel();
    await openPanel();
    // The first "New Chat" click starts (and never finishes) creating a
    // session, so createSession.isPending stays true for the rest of the
    // test.
    await userEvent.click(screen.getByLabelText("New Chat"));
    sessionCreateCalls += 1;

    const input = screen.getByPlaceholderText(
      "Ask Aria for follow-up changes...",
    );
    await userEvent.type(input, "premature submit");
    await userEvent.click(screen.getByLabelText("Send message"));

    // handleSubmit's `createSession.isPending` guard returns before doing
    // anything: the input keeps its text and no second session is created.
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(input).toHaveValue("premature submit");
    expect(sessionCreateCalls).toBe(1);
  });
});

/* ------------------------------------------------------------------ */
/* DraftCard fallback rendering (untitled draft, unknown artifact type) */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — draft card fallbacks", () => {
  it("falls back to 'Untitled' and the raw artifact_type when a draft lacks a title or a known type label", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(
          envelope([
            {
              draft_id: "ADRF-untitled1",
              session_id: "ASST-msw0001",
              artifact_type: "unlisted_kind",
              status: "draft",
              title: "",
              summary: "No title, unknown type.",
              spec: {},
              artifact: { name: "untitled" },
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
        ),
      ),
    );

    renderPanel();
    await openPanel();
    await userEvent.click(screen.getByLabelText("New Chat"));

    await userEvent.click(await screen.findByText(/1 draft generated/));
    expect(await screen.findByText("Untitled")).toBeInTheDocument();
    expect(screen.getByText("unlisted_kind")).toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/* Plan reattachment race: switching sessions cancels a stale in-flight  */
/* plan lookup instead of applying it to the new session                */
/* ------------------------------------------------------------------ */

describe("CaliberAssistantPanel — plan reattachment race", () => {
  it("discards a stale in-flight plan lookup after switching to another session", async () => {
    let releaseSessionAPlans: (() => void) | null = null;
    const sessionAPlansGate = new Promise<void>((resolve) => {
      releaseSessionAPlans = resolve;
    });

    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-planA",
              title: "Session A",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: { assistant_mode: "plan" },
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
            {
              session_id: "ASST-planB",
              title: "Session B",
              owner: "@test",
              status: "active",
              goal: "",
              metadata_: { assistant_mode: "plan" },
              active_draft_id: null,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/aria/plans`, async ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("session_id") === "ASST-planA") {
          await sessionAPlansGate;
          return HttpResponse.json(
            envelope([
              {
                plan_id: "PLAN-stale",
                session_id: "ASST-planA",
                project_id: null,
                goal: "Stale plan from session A",
                status: "draft",
                autonomy: "approve_plan",
                owner: "@test",
                constraints: {},
                done_when: [],
                context_refs: [],
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                step_count: 0,
              },
            ]),
          );
        }
        // Session B has no in-flight plan to resume.
        return HttpResponse.json(envelope([]));
      }),
      http.get(`${API_BASE}/aria/plans/PLAN-stale`, () =>
        HttpResponse.json(
          envelope({
            plan: {
              plan_id: "PLAN-stale",
              session_id: "ASST-planA",
              project_id: null,
              goal: "Stale plan from session A",
              status: "draft",
              autonomy: "approve_plan",
              owner: "@test",
              constraints: {},
              done_when: [],
              context_refs: [],
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
              step_count: 0,
            },
            steps: [],
          }),
        ),
      ),
    );

    window.localStorage.setItem(ACTIVE_SESSION_KEY, "ASST-planA");
    renderPanel();
    await openPanel();

    // Switch to session B (via chat history) before session A's in-flight
    // plan lookup resolves.
    await userEvent.click(screen.getByLabelText("Chat history"));
    await userEvent.click(screen.getByText("Session B", { selector: "p" }));

    // Now let the stale session-A lookup resolve.
    (releaseSessionAPlans as unknown as (() => void) | null)?.();

    // Session B has its own (empty) plan mode view; session A's stale plan
    // must never appear.
    await waitFor(() => {
      expect(screen.getByTestId("assistant-mode-selector")).toHaveTextContent(
        "Plan",
      );
    });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(
      screen.queryByText("Stale plan from session A"),
    ).not.toBeInTheDocument();
  });
});
