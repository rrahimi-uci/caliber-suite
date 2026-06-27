import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { Settings } from "@/pages/Settings";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const DEFAULT_SKILL_MODE_KEY = "caliber.assistant.defaults.skillMode";
const ALLURE_URL_KEY = "caliber.allure.reportUrl";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderPage(): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/settings"]}>
        <Routes>
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  window.localStorage.removeItem(DEFAULT_SKILL_MODE_KEY);
  window.localStorage.removeItem(ALLURE_URL_KEY);
});

afterAll(() => {
  server.close();
  vi.restoreAllMocks();
});

describe("Settings page", () => {
  it("saves assistant runtime settings and stores the default skill mode locally", async () => {
    let assistantPayload: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/assistant/config`, () =>
        HttpResponse.json(
          envelope({
            engine: "openai",
            model: "gpt-4o-mini",
            provider: "openai",
            reasoning: "medium",
            enabled: true,
            disabled_intents: [],
            disabled_domains: [],
            available_models: [
              { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
              {
                id: "claude-sonnet-4-20250514",
                name: "Claude Sonnet 4",
                provider: "anthropic",
              },
              { id: "qwen2.5:7b", name: "qwen2.5:7b", provider: "ollama" },
            ],
          }),
        ),
      ),
      http.patch(`${API_BASE}/assistant/config`, async ({ request }) => {
        assistantPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            engine: "openai",
            model: "claude-sonnet-4-20250514",
            provider: "anthropic",
            reasoning: "high",
            enabled: true,
            disabled_intents: ["create_tool"],
            disabled_domains: ["tool"],
            available_models: [
              { id: "gpt-5", name: "gpt-5", provider: "openai" },
              {
                id: "claude-sonnet-4-20250514",
                name: "Claude Sonnet 4",
                provider: "anthropic",
              },
            ],
          }),
        );
      }),
    );

    renderPage();

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    // Ollama models the server discovered show up grouped under their own optgroup.
    expect(await screen.findByRole("option", { name: "qwen2.5:7b" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Ollama (local)" })).toBeInTheDocument();
    await userEvent.selectOptions(
      await screen.findByLabelText("Model"),
      "claude-sonnet-4-20250514",
    );
    await userEvent.click(screen.getByRole("button", { name: "High" }));
    await userEvent.click(screen.getByRole("button", { name: "Manual" }));
    await userEvent.click(screen.getByRole("button", { name: "Save assistant settings" }));

    await waitFor(() =>
      expect(assistantPayload).toEqual({
        model: "claude-sonnet-4-20250514",
        reasoning: "high",
      }),
    );
    expect(window.localStorage.getItem(DEFAULT_SKILL_MODE_KEY)).toBe("manual");
  });

  it("saves provider keys from the Providers tab", async () => {
    let providerPayload: Record<string, unknown> | null = null;
    server.use(
      http.patch(`${API_BASE}/settings/llm`, async ({ request }) => {
        providerPayload = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            llm_provider: "openai",
            gateway_url: "",
            openai_key_env: "OPENAI_API_KEY",
            openai_key_present: true,
            anthropic_key_present: true,
            assistant_engine: "openai",
          }),
        );
      }),
    );

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Providers" }));
    await userEvent.type(await screen.findByLabelText("OpenAI API key"), "sk-openai-test");
    await userEvent.type(await screen.findByLabelText("Anthropic API key"), "sk-ant-test");
    const gatewayInput = await screen.findByLabelText("Gateway URL");
    await userEvent.clear(gatewayInput);
    await userEvent.click(screen.getByRole("button", { name: "Save provider settings" }));

    await waitFor(() =>
      expect(providerPayload).toEqual({
        openai_api_key: "sk-openai-test",
        anthropic_api_key: "sk-ant-test",
        gateway_url: "",
      }),
    );
  });

  it("shows backing-service health on the Services tab", async () => {
    server.use(
      http.get(`${API_BASE}/system/services`, () =>
        HttpResponse.json(
          envelope({
            checked_at_ms: 1_700_000_000_000,
            services: [
              {
                key: "mlflow",
                name: "MLflow Tracking",
                description: "Experiments, runs, traces.",
                category: "Tracking",
                url: "http://localhost:5000",
                target: "http://mlflow:5000",
                healthy: true,
                detail: "HTTP 200",
                latency_ms: 12,
              },
              {
                key: "event_bus",
                name: "Event Bus (NATS)",
                description: "Workflow run event fan-out.",
                category: "Messaging",
                url: null,
                target: "nats:4222",
                healthy: false,
                detail: "connection refused",
                latency_ms: null,
              },
            ],
          }),
        ),
      ),
    );

    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Services" }));

    const rows = await screen.findAllByTestId("service-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("MLflow Tracking");
    expect(rows[0]?.textContent).toContain("Healthy");
    expect(rows[0]?.textContent).toContain("http://localhost:5000");
    expect(rows[1]?.textContent).toContain("Down");
    expect(rows[1]?.textContent).toContain("connection refused");
  });

  it("defaults the Allure link to the in-app served report", async () => {
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Allure Report" }));
    const openLink = await screen.findByRole("link", { name: "Open Allure report" });
    expect(openLink.getAttribute("href")).toContain(
      "/ajax-api/2.0/mlflow/caliber/observability/allure-report/",
    );
  });

  it("enables the Allure link when URL is entered as host:port", async () => {
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Allure Report" }));
    const input = await screen.findByLabelText("Allure report URL");
    await userEvent.clear(input);
    await userEvent.type(input, "localhost:9999");

    const openLink = screen.getByRole("link", { name: "Open Allure report" });
    expect(openLink).toHaveAttribute("href", "http://localhost:9999/");
  });

  it("enables the Allure link when URL already includes protocol (case-insensitive)", async () => {
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Allure Report" }));
    const input = await screen.findByLabelText("Allure report URL");
    await userEvent.clear(input);
    await userEvent.type(input, "HTTP://localhost:5252/allure");

    const openLink = screen.getByRole("link", { name: "Open Allure report" });
    expect(openLink).toHaveAttribute("href", "http://localhost:5252/allure");
  });
});
