/**
 * Coverage for ChatHistory (list render, select / rename / archive actions,
 * active-row state, empty state) and AssistantSettings (config-driven form
 * fields, save, error, and the modal close paths).
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AssistantSettings } from "@/components/assistant/AssistantSettings";
import { ChatHistory } from "@/components/assistant/ChatHistory";
import type { AssistantConfig, AssistantSession } from "@/api/assistantTypes";
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
    <QueryClientProvider client={queryClient}>{node}</QueryClientProvider>,
  );
}

function makeSession(overrides: Partial<AssistantSession> = {}): AssistantSession {
  return {
    session_id: "ASST-1",
    title: "First chat",
    owner: "@test",
    status: "active",
    goal: "",
    metadata_: {},
    active_draft_id: null,
    created_at: new Date("2025-01-01T00:00:00Z").toISOString(),
    updated_at: new Date("2025-01-02T00:00:00Z").toISOString(),
    ...overrides,
  };
}

function makeConfig(overrides: Partial<AssistantConfig> = {}): AssistantConfig {
  return {
    engine: "fake",
    model: "gpt-4o-mini",
    provider: "openai",
    reasoning: "medium",
    enabled: true,
    disabled_intents: ["propose_promotion"],
    disabled_domains: ["mcp_server"],
    available_models: [
      { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
      { id: "gpt-5.4", name: "GPT-5.4", provider: "openai" },
    ],
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("ChatHistory", () => {
  it("renders the empty state when there are no non-archived sessions", () => {
    withProviders(
      <ChatHistory
        sessions={[]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
      />,
    );
    expect(screen.getByText("No sessions yet.")).toBeInTheDocument();
    expect(screen.getByText("Chat history (0)")).toBeInTheDocument();
  });

  it("hides archived sessions from the visible list and count", () => {
    withProviders(
      <ChatHistory
        sessions={[
          makeSession({ session_id: "ASST-a", title: "Active one" }),
          makeSession({ session_id: "ASST-b", title: "Gone", status: "archived" }),
        ]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
      />,
    );
    expect(screen.getByText("Chat history (1)")).toBeInTheDocument();
    expect(screen.getByText("Active one")).toBeInTheDocument();
    expect(screen.queryByText("Gone")).not.toBeInTheDocument();
  });

  it("renders the title fallback and selects a row on click", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const session = makeSession({ session_id: "ASST-x", title: "" });
    withProviders(
      <ChatHistory
        sessions={[session]}
        activeSessionId={null}
        onSelect={onSelect}
        onNewChat={vi.fn()}
      />,
    );
    // Empty title falls back to "Untitled".
    await user.click(screen.getByText("Untitled"));
    expect(onSelect).toHaveBeenCalledWith(session);
  });

  it("marks the active row with the active border styling", () => {
    withProviders(
      <ChatHistory
        sessions={[
          makeSession({ session_id: "ASST-active", title: "Active row" }),
          makeSession({ session_id: "ASST-other", title: "Other row" }),
        ]}
        activeSessionId="ASST-active"
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
      />,
    );
    const activeRow = screen.getByText("Active row").closest("div.rounded-lg");
    const otherRow = screen.getByText("Other row").closest("div.rounded-lg");
    expect(activeRow?.className).toContain("border-caliber-300");
    expect(otherRow?.className).not.toContain("border-caliber-300");
  });

  it("fires onNewChat when '+ New chat' is clicked", async () => {
    const user = userEvent.setup();
    const onNewChat = vi.fn();
    withProviders(
      <ChatHistory
        sessions={[]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNewChat={onNewChat}
      />,
    );
    await user.click(screen.getByRole("button", { name: "+ New chat" }));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it("renames a session: opens the inline form, edits, and PATCHes the title", async () => {
    const user = userEvent.setup();
    let patchedTitle: unknown = null;
    server.use(
      http.patch(`${API_BASE}/assistant/sessions/:sessionId`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchedTitle = body.title;
        return HttpResponse.json(
          envelope(makeSession({ session_id: String(params.sessionId), title: "Renamed chat" })),
        );
      }),
    );

    withProviders(
      <ChatHistory
        sessions={[makeSession({ session_id: "ASST-r", title: "Before" })]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Rename session" }));
    const input = screen.getByDisplayValue("Before");
    await user.clear(input);
    await user.type(input, "Renamed chat");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchedTitle).toBe("Renamed chat"));
    // onSuccess closes the editing form, returning to the read-only row.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument(),
    );
  });

  it("renames to 'Untitled' when the draft title is cleared to whitespace", async () => {
    const user = userEvent.setup();
    let patchedTitle: unknown = "unset";
    server.use(
      http.patch(`${API_BASE}/assistant/sessions/:sessionId`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchedTitle = body.title;
        return HttpResponse.json(
          envelope(makeSession({ session_id: String(params.sessionId) })),
        );
      }),
    );

    withProviders(
      <ChatHistory
        sessions={[makeSession({ session_id: "ASST-r2", title: "Has a title" })]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Rename session" }));
    await user.clear(screen.getByDisplayValue("Has a title"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchedTitle).toBe("Untitled"));
  });

  it("archives a session via the archive button (status PATCH)", async () => {
    const user = userEvent.setup();
    let patchedStatus: unknown = null;
    server.use(
      http.patch(`${API_BASE}/assistant/sessions/:sessionId`, async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchedStatus = body.status;
        return HttpResponse.json(
          envelope(makeSession({ session_id: String(params.sessionId), status: "archived" })),
        );
      }),
    );

    withProviders(
      <ChatHistory
        sessions={[makeSession({ session_id: "ASST-arch", title: "Archive me" })]}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNewChat={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Archive session" }));
    await waitFor(() => expect(patchedStatus).toBe("archived"));
  });
});

describe("AssistantSettings", () => {
  it("shows the loading state before config resolves", async () => {
    let resolve!: () => void;
    const gate = new Promise<void>((r) => {
      resolve = r;
    });
    server.use(
      http.get(`${API_BASE}/assistant/config`, async () => {
        await gate;
        return HttpResponse.json(envelope(makeConfig()));
      }),
    );

    withProviders(<AssistantSettings onClose={vi.fn()} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();

    resolve();
    expect(await screen.findByText("Model")).toBeInTheDocument();
  });

  it("populates the form fields from the loaded config", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/config`, () => HttpResponse.json(envelope(makeConfig()))),
    );

    withProviders(<AssistantSettings onClose={vi.fn()} />);

    const modelSelect = (await screen.findByText("Model"))
      .closest("label")
      ?.querySelector("select") as HTMLSelectElement;
    expect(modelSelect.value).toBe("gpt-4o-mini");
    expect(within(modelSelect).getByText("GPT-5.4 (OpenAI)")).toBeInTheDocument();

    expect(screen.getByDisplayValue("propose_promotion")).toBeInTheDocument();
    expect(screen.getByDisplayValue("mcp_server")).toBeInTheDocument();
    expect(screen.getByText(/Engine: fake · Provider: openai/)).toBeInTheDocument();
  });

  it("edits fields and PATCHes the parsed CSV payload on Save, then closes", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    let patchBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/assistant/config`, () => HttpResponse.json(envelope(makeConfig()))),
      http.patch(`${API_BASE}/assistant/config`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(makeConfig()));
      }),
    );

    withProviders(<AssistantSettings onClose={onClose} />);
    await screen.findByText("Model");

    const modelSelect = screen
      .getByText("Model")
      .closest("label")
      ?.querySelector("select") as HTMLSelectElement;
    await user.selectOptions(modelSelect, "gpt-5.4");

    const reasoningSelect = screen
      .getByText("Reasoning effort")
      .closest("label")
      ?.querySelector("select") as HTMLSelectElement;
    await user.selectOptions(reasoningSelect, "high");

    const intents = screen.getByPlaceholderText("e.g. propose_promotion");
    await user.clear(intents);
    await user.type(intents, " run_prompt_optimization , , propose_promotion ");

    const domains = screen.getByPlaceholderText("e.g. mcp_server");
    await user.clear(domains);
    await user.type(domains, "mcp_server,");

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toMatchObject({
      model: "gpt-5.4",
      reasoning: "high",
      // splitCsv trims, drops blanks.
      disabled_intents: ["run_prompt_optimization", "propose_promotion"],
      disabled_domains: ["mcp_server"],
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("surfaces a save error and keeps the modal open", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    server.use(
      http.get(`${API_BASE}/assistant/config`, () => HttpResponse.json(envelope(makeConfig()))),
      http.patch(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json({ message: "operator scope required" }, { status: 403 }),
      ),
    );

    withProviders(<AssistantSettings onClose={onClose} />);
    await screen.findByText("Model");

    await user.click(screen.getByRole("button", { name: "Save" }));

    // onError fires (no close) — the modal stays mounted.
    await waitFor(() => expect(screen.getByText("Save")).toBeInTheDocument());
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes via the header X button without saving", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    server.use(
      http.get(`${API_BASE}/assistant/config`, () => HttpResponse.json(envelope(makeConfig()))),
    );

    withProviders(<AssistantSettings onClose={onClose} />);
    await screen.findByText("Model");

    await user.click(screen.getByRole("button", { name: "Close settings" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes via the footer Cancel button", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    server.use(
      http.get(`${API_BASE}/assistant/config`, () => HttpResponse.json(envelope(makeConfig()))),
    );

    withProviders(<AssistantSettings onClose={onClose} />);
    await screen.findByText("Model");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
