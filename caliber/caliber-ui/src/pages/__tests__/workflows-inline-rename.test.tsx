import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { Workflows } from "@/pages/Workflows";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
});
afterAll(() => server.close());

describe("Workflows inline rename submit", () => {
  it("submits inline rename on Enter and exits edit mode for unchanged and Escape", async () => {
    const user = userEvent.setup();
    const baseWorkflow = {
      workflow_id: "WF-1",
      name: "Workflow One",
      description: "desc",
      owner: "@test",
      status: "active" as const,
      default_experiment_id: null,
      created_at: "2025-01-01T00:00:00Z",
      updated_at: "2025-01-01T00:00:00Z",
    };

    let currentName = baseWorkflow.name;
    let renameCalls = 0;

    server.use(
      http.get(`${API_BASE}/workflows`, () =>
        HttpResponse.json(envelope([{ ...baseWorkflow, name: currentName }])),
      ),
      http.patch(`${API_BASE}/workflows/:workflowId`, async ({ request }) => {
        const body = (await request.json()) as { name?: string };
        renameCalls += 1;
        currentName = body.name ?? currentName;
        return HttpResponse.json(envelope({ ...baseWorkflow, name: currentName }));
      }),
    );

    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/workflows"]}>
          <Routes>
            <Route path="/workflows" element={<Workflows />} />
            <Route path="/workflows/:workflowId" element={<div>Workflow detail</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText("Workflow One");

    await user.click(screen.getByTestId("edit-workflow-WF-1"));
    const input = screen.getByPlaceholderText("Workflow name");
    await user.clear(input);
    await user.type(input, "Workflow Renamed{Enter}");

    await waitFor(() => {
      expect(renameCalls).toBe(1);
    });
    expect(await screen.findByText("Workflow Renamed")).toBeInTheDocument();

    await user.click(screen.getByTestId("edit-workflow-WF-1"));
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(screen.queryByPlaceholderText("Workflow name")).not.toBeInTheDocument();
    });

    await user.click(screen.getByTestId("edit-workflow-WF-1"));
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByPlaceholderText("Workflow name")).not.toBeInTheDocument();
    });
  });
});
