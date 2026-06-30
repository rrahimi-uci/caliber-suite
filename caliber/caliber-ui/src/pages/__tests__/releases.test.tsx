import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Releases } from "@/pages/Releases";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderPage(): void {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Releases />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Releases", () => {
  it("renders the live board and the promotion/rollback timeline", async () => {
    server.use(
      http.get(`${API_BASE}/releases/live`, () =>
        HttpResponse.json(
          envelope([
            {
              artifact_type: "workflow",
              artifact_id: "WF-1",
              alias: "prod",
              version_id: "WFV-7",
              since: "2026-06-29T00:00:00Z",
              by: "@reza",
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/releases/timeline`, () =>
        HttpResponse.json(
          envelope([
            {
              log_id: 2,
              timestamp: "2026-06-30T00:00:00Z",
              actor: "@reza",
              action: "rollback_prompt",
              entity_type: "prompt",
              entity_id: "support-agent",
              details: { from_version: 7, to_version: 6, overridden: true },
            },
            {
              log_id: 1,
              timestamp: "2026-06-29T00:00:00Z",
              actor: "@sam",
              action: "promote_workflow",
              entity_type: "workflow",
              entity_id: "WF-1",
              details: { to_version: "WFV-7" },
            },
          ]),
        ),
      ),
    );

    renderPage();

    // Live board.
    expect(await screen.findByTestId("releases-live-WF-1")).toHaveTextContent("WFV-7");
    // Timeline events.
    const rollback = await screen.findByTestId("releases-event-2");
    expect(rollback).toHaveTextContent("Roll back");
    expect(rollback).toHaveTextContent("support-agent");
    expect(rollback).toHaveTextContent("v7 → v6");
    // Overridden-gate badge surfaces.
    expect(screen.getByTestId("releases-overridden-2")).toBeInTheDocument();
    expect(screen.getByTestId("releases-event-1")).toHaveTextContent("Promote");
  });

  it("shows empty states when nothing is live or recorded", async () => {
    server.use(
      http.get(`${API_BASE}/releases/live`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/releases/timeline`, () => HttpResponse.json(envelope([]))),
    );
    renderPage();
    expect(await screen.findByText("Nothing deployed yet.")).toBeInTheDocument();
    expect(screen.getByText("No promotions or rollbacks yet.")).toBeInTheDocument();
  });
});
