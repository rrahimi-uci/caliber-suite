import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { EvalDatasets } from "@/pages/EvalDatasets";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-07T18:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeDataset(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    dataset_id: "DS-1",
    name: "factual-checks",
    description: "Curated Q&A pairs that test factual recall.",
    owner: "@sarah",
    tags: ["qa", "facts"],
    status: "active" as const,
    version: 1,
    created_at: NOW,
    updated_at: NOW,
    mlflow_dataset_id: null,
    mlflow_synced_at: null,
    mlflow_synced_version: null,
    mlflow_record_count: null,
    mlflow_digest: null,
    ...overrides,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/eval-datasets"]}
    >
      <Routes>
        <Route path="/eval-datasets" element={<EvalDatasets />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("EvalDatasets create panel (lines 399-485)", () => {
  it("toggles the create panel open and closed via the header button", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([makeDataset()])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();

    // Panel is hidden until the header button opens it.
    expect(screen.queryByText("New test set")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "+ New Test Set" }));
    expect(screen.getByText("New test set")).toBeInTheDocument();
    // Both the header toggle and the panel footer now show a "Cancel" button.
    const cancels = screen.getAllByRole("button", { name: "Cancel" });
    expect(cancels).toHaveLength(2);

    // The header "Cancel" toggle (first in DOM order) closes the panel.
    await user.click(cancels[0]!);
    expect(screen.queryByText("New test set")).not.toBeInTheDocument();
  });

  it("keeps Create disabled until both name and owner are filled", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([makeDataset()])),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "+ New Test Set" }));
    const panel = screen.getByText("New test set").closest("div");
    expect(panel).not.toBeNull();
    const scope = within(panel as HTMLElement);

    const createBtn = scope.getByRole("button", { name: "Create" });
    expect(createBtn).toBeDisabled();

    // Name alone is not enough.
    await user.type(scope.getByPlaceholderText("factual-checks"), "tone-grading");
    expect(createBtn).toBeDisabled();

    // Owner completes the required pair → button enables.
    await user.type(scope.getByPlaceholderText("@sarah"), "@dana");
    expect(createBtn).toBeEnabled();
  });

  it("submits the form, posts the payload, closes the panel and refreshes", async () => {
    let postBody: Record<string, unknown> | null = null;
    let created = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          envelope(
            created
              ? [
                  makeDataset(),
                  makeDataset({
                    dataset_id: "DS-2",
                    name: "tone-grading",
                    owner: "@dana",
                    tags: [],
                  }),
                ]
              : [makeDataset()],
          ),
        ),
      ),
      http.post(`${API_BASE}/eval-datasets`, async ({ request }) => {
        postBody = (await request.json()) as Record<string, unknown>;
        created = true;
        return HttpResponse.json(
          envelope(
            makeDataset({
              dataset_id: "DS-2",
              name: "tone-grading",
              owner: "@dana",
              tags: [],
            }),
          ),
        );
      }),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "+ New Test Set" }));
    const panel = screen.getByText("New test set").closest("div");
    const scope = within(panel as HTMLElement);

    await user.type(scope.getByPlaceholderText("factual-checks"), "tone-grading");
    await user.type(scope.getByPlaceholderText("@sarah"), "@dana");
    await user.type(
      scope.getByPlaceholderText("Curated Q&A pairs that test factual recall."),
      "Style and tone rubric set.",
    );

    await user.click(scope.getByRole("button", { name: "Create" }));

    // On success the panel closes and the refreshed list shows the new row.
    await waitFor(() =>
      expect(screen.queryByText("New test set")).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("tone-grading")).toBeInTheDocument();

    expect(postBody).toEqual({
      name: "tone-grading",
      description: "Style and tone rubric set.",
      owner: "@dana",
      tags: [],
    });
  });

  it("surfaces the API error inside the panel without closing it", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([makeDataset()])),
      ),
      http.post(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(
          { detail: "name already taken" },
          { status: 409 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "+ New Test Set" }));
    const panel = screen.getByText("New test set").closest("div");
    const scope = within(panel as HTMLElement);

    await user.type(scope.getByPlaceholderText("factual-checks"), "factual-checks");
    await user.type(scope.getByPlaceholderText("@sarah"), "@sarah");
    await user.click(scope.getByRole("button", { name: "Create" }));

    // The error is rendered inline and the panel stays open.
    expect(await screen.findByText("name already taken")).toBeInTheDocument();
    expect(screen.getByText("New test set")).toBeInTheDocument();
    expect(scope.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("dismisses the panel with its own Cancel button without posting", async () => {
    let posted = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets`, () =>
        HttpResponse.json(envelope([makeDataset()])),
      ),
      http.post(`${API_BASE}/eval-datasets`, () => {
        posted = true;
        return HttpResponse.json(envelope(makeDataset()));
      }),
    );

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("factual-checks")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "+ New Test Set" }));
    const panel = screen.getByText("New test set").closest("div");
    const scope = within(panel as HTMLElement);

    await user.type(scope.getByPlaceholderText("factual-checks"), "draft-set");
    // The panel's footer Cancel button closes it without a POST.
    await user.click(scope.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByText("New test set")).not.toBeInTheDocument(),
    );
    expect(posted).toBe(false);
  });
});
