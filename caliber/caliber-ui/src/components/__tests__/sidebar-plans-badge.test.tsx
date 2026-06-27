import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makePlan(status: string, plan_id: string) {
  return {
    plan_id,
    session_id: null,
    goal: "g",
    status,
    autonomy: "approve_plan",
    owner: "@me",
    created_at: "",
    updated_at: "",
    step_count: 0,
  };
}

function renderSidebar(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Sidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("Sidebar plans badge", () => {
  it("badges the Plans nav with the count of plans awaiting input", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(
          envelope([
            makePlan("paused", "A"),
            makePlan("completed", "B"),
            makePlan("paused", "C"),
            makePlan("running", "D"),
          ]),
        ),
      ),
    );
    renderSidebar();
    const badge = await screen.findByLabelText(/awaiting your input/i);
    expect(badge).toHaveTextContent("2");
  });

  it("shows no badge when nothing is paused", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(envelope([makePlan("running", "A"), makePlan("completed", "B")])),
      ),
    );
    renderSidebar();
    expect(await screen.findByText("Plans")).toBeInTheDocument();
    expect(screen.queryByLabelText(/awaiting your input/i)).toBeNull();
  });

  it("links Docs to the documentation HTML in a new tab", async () => {
    server.use(http.get(`${API_BASE}/aria/plans`, () => HttpResponse.json(envelope([]))));
    renderSidebar();
    const docs = await screen.findByRole("link", { name: "Docs" });
    expect(docs).toHaveAttribute("target", "_blank");
    expect(docs.getAttribute("href")).toMatch(/docs\/index\.html$/);
    expect(docs.getAttribute("rel") ?? "").toContain("noopener");
  });
});
