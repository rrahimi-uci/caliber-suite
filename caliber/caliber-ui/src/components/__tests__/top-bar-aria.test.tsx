import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import { AssistantPanelProvider } from "@/components/assistant/AssistantPanelContext";
import { TopBar } from "@/components/TopBar";
import { capabilitiesBody } from "@/test/handlers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

vi.mock("@/components/WorkspaceSelector", () => ({
  WorkspaceSelector: () => <span>Workspace</span>,
}));

function config(extra: Record<string, unknown>): {
  data: Record<string, unknown>;
} {
  return {
    data: {
      engine: "openai",
      model: "gpt-5.6-luna",
      provider: "openai",
      reasoning: "high",
      enabled: true,
      disabled_intents: [],
      disabled_domains: [],
      available_models: [],
      ...extra,
    },
  };
}

function renderTopBar(): void {
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <AssistantPanelProvider>
        <TopBar health="ok" currentUser="@ana" onLogout={vi.fn()} />
      </AssistantPanelProvider>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("TopBar Ask Aria", () => {
  it("is disabled, with the reason, when no engine is configured (alpha P1-11)", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          config({
            configured: false,
            reason: "Aria needs an LLM API key, and OPENAI_API_KEY is not set.",
          }),
        ),
      ),
    );
    renderTopBar();

    const button = await screen.findByRole("button", { name: "Ask Aria" });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAttribute(
      "title",
      expect.stringContaining("OPENAI_API_KEY is not set"),
    );
    expect(button).toHaveAccessibleDescription(
      expect.stringContaining("OPENAI_API_KEY is not set"),
    );
  });

  it("falls back to a generic reason when the server gives none", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(config({ configured: false, reason: null })),
      ),
    );
    renderTopBar();

    const button = await screen.findByRole("button", { name: "Ask Aria" });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAttribute(
      "title",
      "Aria is not configured on this deployment.",
    );
  });

  it("stays available when an engine is configured", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(config({ configured: true, reason: null })),
      ),
    );
    renderTopBar();

    const button = await screen.findByRole("button", { name: "Ask Aria" });
    // Give the query time to settle; the button must never have been disabled.
    await waitFor(() => expect(button).toBeEnabled());
    expect(button).not.toHaveAttribute("title");
  });

  it("stays available against a server that does not report configured", async () => {
    // The default test handler predates the field.
    renderTopBar();

    const button = await screen.findByRole("button", { name: "Ask Aria" });
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("stays available while the configuration is unreadable", async () => {
    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderTopBar();

    expect(await screen.findByRole("button", { name: "Ask Aria" })).toBeEnabled();
  });
});

/** Let the capabilities request finish and its re-render land, inside ``act``. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

describe("TopBar Ask Aria and the aria feature flag", () => {
  it("has no Ask Aria button, and never reads the assistant config, when the flag is off", async () => {
    let configReads = 0;
    server.use(
      http.get(`${API_BASE}/capabilities`, () => HttpResponse.json(capabilitiesBody({}))),
      http.get(`${API_BASE}/assistant/config`, () => {
        configReads += 1;
        return HttpResponse.json(config({}));
      }),
    );
    renderTopBar();

    // The rest of the bar is there; Aria is not, and stays absent once capabilities have loaded.
    expect(screen.getByRole("button", { name: /Switch to (dark|light) mode/ })).toBeInTheDocument();
    await settle();
    expect(screen.queryByRole("button", { name: "Ask Aria" })).not.toBeInTheDocument();
    expect(configReads).toBe(0);
  });

  it("has no Ask Aria button while capabilities are loading, or when they cannot be loaded", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderTopBar();

    expect(screen.queryByRole("button", { name: "Ask Aria" })).not.toBeInTheDocument();
    await settle();
    expect(screen.queryByRole("button", { name: "Ask Aria" })).not.toBeInTheDocument();
  });
});
