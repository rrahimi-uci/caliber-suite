/**
 * Tests for Aria's code-assistant capabilities: mode selector, context
 * attachments ("+ add files"), chat history, and the settings modal.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { CaliberAssistantPanel } from "@/components/assistant/CaliberAssistantPanel";
import { AttachmentBar } from "@/components/assistant/AttachmentBar";
import { ModeSelector } from "@/components/assistant/ModeSelector";
import {
  AssistantPanelProvider,
  useAssistantPanel,
} from "@/components/assistant/AssistantPanelContext";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function withProviders(node: JSX.Element): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        {node}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderPanel(): void {
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
  withProviders(
    <AssistantPanelProvider>
      <Inner />
    </AssistantPanelProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("ModeSelector", () => {
  it("renders the three modes and reports a selection", async () => {
    const onChange = vi.fn();
    withProviders(<ModeSelector value="build" onChange={onChange} />);
    const selector = screen.getByTestId("assistant-mode-selector");
    expect(selector).toBeInTheDocument();
    expect(selector).toHaveTextContent("Design");
    await userEvent.click(selector);
    await userEvent.click(screen.getByText("Plan"));
    expect(onChange).toHaveBeenCalledWith("plan");
  });
});

describe("AttachmentBar", () => {
  it("attaches a pasted text snippet and lists it", async () => {
    let created = false;
    server.use(
      http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () =>
        HttpResponse.json(
          envelope(
            created
              ? [
                  {
                    attachment_id: "AATT-1",
                    session_id: "ASST-1",
                    kind: "text_snippet",
                    ref_type: "",
                    ref_id: "",
                    name: "My notes",
                    content_text: "hello",
                    bytes_size: 5,
                    truncated: false,
                    metadata_: {},
                    created_by: "@test",
                    created_at: new Date().toISOString(),
                  },
                ]
              : [],
          ),
        ),
      ),
      http.post(`${API_BASE}/assistant/sessions/:sessionId/attachments`, async () => {
        created = true;
        return HttpResponse.json(
          envelope({
            attachment_id: "AATT-1",
            session_id: "ASST-1",
            kind: "text_snippet",
            ref_type: "",
            ref_id: "",
            name: "My notes",
            content_text: "hello",
            bytes_size: 5,
            truncated: false,
            metadata_: {},
            created_by: "@test",
            created_at: new Date().toISOString(),
          }),
          { status: 201 },
        );
      }),
    );

    const ensureSession = vi.fn().mockResolvedValue("ASST-1");
    withProviders(<AttachmentBar sessionId="ASST-1" ensureSession={ensureSession} />);

    await userEvent.click(screen.getByTestId("assistant-add-context"));
    await userEvent.click(screen.getByText("Paste text"));

    const modal = await screen.findByTestId("assistant-text-modal");
    await userEvent.type(within(modal).getByPlaceholderText("Label (optional)"), "My notes");
    await userEvent.type(within(modal).getByPlaceholderText("Paste context here…"), "hello");
    await userEvent.click(within(modal).getByRole("button", { name: "Attach" }));

    await waitFor(() =>
      expect(screen.getByTestId("assistant-attachment-chips")).toHaveTextContent("My notes"),
    );
  });
});

describe("CaliberAssistantPanel — modes & settings", () => {
  async function openPanel(): Promise<void> {
    renderPanel();
    await userEvent.click(screen.getByText("Open Assistant"));
    await screen.findByText("Aria");
  }

  it("shows the mode selector and sends the selected mode", async () => {
    let sentMode: unknown = null;
    server.use(
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        sentMode = body.mode;
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-1",
              session_id: String(params.sessionId),
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
      }),
    );

    await openPanel();
    expect(screen.getByTestId("assistant-mode-selector")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("assistant-mode-selector"));
    await userEvent.click(screen.getByText("Chat"));
    const box = screen.getByPlaceholderText("Ask Aria for follow-up changes...");
    await userEvent.type(box, "hi there");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(sentMode).toBe("chat"));
  });

  it("sends the selected approval mode", async () => {
    let sentApproval: unknown = null;
    server.use(
      http.post(`${API_BASE}/assistant/sessions/:sessionId/messages`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        sentApproval = body.approval_mode;
        return HttpResponse.json(
          envelope({
            assistant_message: {
              message_id: "AMSG-a1",
              session_id: String(params.sessionId),
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
      }),
    );

    await openPanel();
    expect(screen.getByTestId("assistant-approval-selector")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("assistant-approval-selector"));
    await userEvent.click(screen.getByText("Full access"));
    await userEvent.type(screen.getByPlaceholderText("Ask Aria for follow-up changes..."), "build me a tool");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(sentApproval).toBe("auto_all"));
  });

  it("updates the inline model and reasoning controls", async () => {
    const patchBodies: Record<string, unknown>[] = [];
    let currentConfig = {
      engine: "fake",
      model: "gpt-4o-mini",
      provider: "openai",
      reasoning: "medium",
      enabled: true,
      disabled_intents: [],
      disabled_domains: [],
      available_models: [
        { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" as const },
        { id: "gpt-5.4", name: "GPT-5.4", provider: "openai" as const },
      ],
    };

    server.use(
      http.get(`${API_BASE}/assistant/config`, () => HttpResponse.json(envelope(currentConfig))),
      http.patch(`${API_BASE}/assistant/config`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchBodies.push(body);
        currentConfig = {
          ...currentConfig,
          model: typeof body.model === "string" ? body.model : currentConfig.model,
          reasoning:
            typeof body.reasoning === "string" ? body.reasoning : currentConfig.reasoning,
        };
        return HttpResponse.json(envelope(currentConfig));
      }),
    );

    await openPanel();
    expect(await screen.findByTestId("assistant-model-selector")).toHaveTextContent("4o mini");

    await userEvent.click(screen.getByTestId("assistant-model-selector"));
    await userEvent.click(screen.getByText("GPT-5.4"));
    await waitFor(() => expect(patchBodies[0]).toMatchObject({ model: "gpt-5.4" }));
    await waitFor(() => expect(screen.getByTestId("assistant-model-selector")).toHaveTextContent("5.4"));

    await userEvent.click(screen.getByTestId("assistant-model-selector"));
    await userEvent.click(screen.getByText("High"));
    await waitFor(() => expect(patchBodies[1]).toMatchObject({ reasoning: "high" }));
  });

  it("opens the settings modal", async () => {
    await openPanel();
    await userEvent.click(screen.getByRole("button", { name: "Aria settings" }));
    expect(await screen.findByTestId("assistant-settings")).toBeInTheDocument();
    expect(screen.getByText("Aria settings")).toBeInTheDocument();
  });

  it("toggles the chat history drawer", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/sessions`, () =>
        HttpResponse.json(
          envelope([
            {
              session_id: "ASST-hist1",
              title: "Older chat",
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
    );
    await openPanel();
    await userEvent.click(screen.getByRole("button", { name: "Chat history" }));
    const history = await screen.findByTestId("assistant-history");
    expect(within(history).getByText("Older chat")).toBeInTheDocument();
  });
});
