import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { EvalDatasetDetail } from "@/pages/EvalDatasetDetail";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-20T12:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function dataset(overrides: Record<string, unknown> = {}) {
  return {
    dataset_id: "ED-1",
    name: "factual-checks",
    description: "Curated Q&A",
    owner: "@sarah",
    tags: ["facts"],
    status: "active",
    version: 2,
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

function example(overrides: Record<string, unknown> = {}) {
  return {
    example_id: "EX-1",
    dataset_id: "ED-1",
    dataset_version: 2,
    input: { input: "What is 2+2?" },
    expected: { expected: "4" },
    weight: 1.0,
    tags: ["golden"],
    created_at: NOW,
    superseded_at: null,
    superseded_version: null,
    ...overrides,
  };
}

function renderPage(): void {
  render(
    <MemoryRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      initialEntries={["/eval-datasets/ED-1"]}
    >
      <Routes>
        <Route path="/eval-datasets/:datasetId" element={<EvalDatasetDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("EvalDatasetDetail", () => {
  it("restores a selected prior version as a new version", async () => {
    let restoredBody: unknown = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/restore`, async ({ request }) => {
        restoredBody = await request.json();
        return HttpResponse.json(envelope(dataset({ version: 3 })));
      }),
    );
    renderPage();
    await screen.findByTestId("eval-dataset-name");
    // No restore button on the default "All" view.
    expect(screen.queryByTestId("restore-version")).not.toBeInTheDocument();
    // Select prior version v1 (current is v2) -> restore affordance appears.
    await userEvent.selectOptions(screen.getByTestId("version-filter"), "1");
    await userEvent.click(await screen.findByTestId("restore-version"));
    await waitFor(() => expect(restoredBody).toEqual({ version: 1 }));
  });

  it("renders the dataset header and its example rows", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example(), example({ example_id: "EX-2", input: { input: "capital of France?" } })])),
      ),
    );
    renderPage();
    expect(await screen.findByTestId("eval-dataset-name")).toHaveTextContent("factual-checks");
    expect(screen.getByTestId("eval-dataset-version")).toHaveTextContent("v2");
    const rows = await screen.findAllByTestId("example-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText(/What is 2\+2\?/)).toBeInTheDocument();
  });

  it("adds a new example by hand (POST /examples with parsed JSON)", async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples`, async ({ request }) => {
        postedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(example({ example_id: "EX-new" })), { status: 201 });
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");

    await userEvent.click(screen.getByTestId("example-add-toggle"));
    const editor = await screen.findByTestId("example-editor");
    fireEvent.change(within(editor).getByTestId("example-input"), {
      target: { value: '{"input": "new q"}' },
    });
    fireEvent.change(within(editor).getByTestId("example-expected"), {
      target: { value: '{"expected": "new a"}' },
    });
    fireEvent.change(within(editor).getByTestId("example-tags"), {
      target: { value: "golden, edge" },
    });
    await userEvent.click(within(editor).getByTestId("example-save"));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedBody).toMatchObject({
      input: { input: "new q" },
      expected: { expected: "new a" },
      tags: ["golden", "edge"],
    });
  });

  it("rejects invalid JSON in the editor without posting", async () => {
    let posted = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples`, () => {
        posted = true;
        return HttpResponse.json(envelope(example()), { status: 201 });
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-add-toggle"));
    const editor = await screen.findByTestId("example-editor");
    fireEvent.change(within(editor).getByTestId("example-input"), {
      target: { value: "not json" },
    });
    await userEvent.click(within(editor).getByTestId("example-save"));
    expect(await within(editor).findByText(/Input:.*valid JSON/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("edits a row via the /revise endpoint", async () => {
    let revisePath: string | null = null;
    let reviseBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(
        `${API_BASE}/eval-datasets/ED-1/examples/:exId/revise`,
        async ({ request, params }) => {
          revisePath = String(params.exId);
          reviseBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(envelope(example({ example_id: "EX-new" })), { status: 201 });
        },
      ),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-edit"));
    const editor = await screen.findByTestId("example-editor");
    // Pre-filled from the existing row; tweak the expected answer.
    fireEvent.change(within(editor).getByTestId("example-expected"), {
      target: { value: '{"expected": "four"}' },
    });
    await userEvent.click(within(editor).getByTestId("example-save"));
    await waitFor(() => expect(reviseBody).not.toBeNull());
    expect(revisePath).toBe("EX-1");
    expect(reviseBody).toMatchObject({ expected: { expected: "four" } });
  });

  it("retires a row via the /supersede endpoint", async () => {
    let supersededId: string | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples/:exId/supersede`, ({ params }) => {
        supersededId = String(params.exId);
        return HttpResponse.json(envelope(example({ superseded_at: NOW, superseded_version: 3 })));
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-retire"));
    await waitFor(() => expect(supersededId).toBe("EX-1"));
  });

  it("re-fetches with include_superseded when 'Show retired' is toggled", async () => {
    let lastIncludeSuperseded: string | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, ({ request }) => {
        lastIncludeSuperseded = new URL(request.url).searchParams.get("include_superseded");
        return HttpResponse.json(envelope([example()]));
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    expect(lastIncludeSuperseded).toBeNull();
    await userEvent.click(screen.getByTestId("show-retired-toggle"));
    await waitFor(() => expect(lastIncludeSuperseded).toBe("true"));
  });
});

describe("EvalDatasetDetail — capture from trace", () => {
  it("captures an example from a trace id (POST /examples/from-trace)", async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples/from-trace`, async ({ request }) => {
        postedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(example({ example_id: "EX-trace" })), { status: 201 });
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");

    await userEvent.click(screen.getByTestId("from-trace-toggle"));
    // Save stays disabled until a trace id is entered.
    expect(screen.getByTestId("from-trace-save")).toBeDisabled();
    fireEvent.change(screen.getByTestId("from-trace-id"), { target: { value: "  tr-abc123  " } });
    expect(screen.getByTestId("from-trace-save")).toBeEnabled();
    await userEvent.click(screen.getByTestId("from-trace-save"));

    await waitFor(() => expect(postedBody).not.toBeNull());
    // The trace id is trimmed before posting.
    expect(postedBody).toMatchObject({ trace_id: "tr-abc123" });
  });

  it("surfaces a capture error inside the trace editor", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples/from-trace`, () =>
        HttpResponse.json({ detail: "trace not found" }, { status: 404 }),
      ),
    );
    renderPage();
    await screen.findAllByTestId("example-row");

    await userEvent.click(screen.getByTestId("from-trace-toggle"));
    fireEvent.change(screen.getByTestId("from-trace-id"), { target: { value: "tr-missing" } });
    await userEvent.click(screen.getByTestId("from-trace-save"));

    expect(await screen.findByText("trace not found")).toBeInTheDocument();
    // The editor remains open and the button is re-enabled after the failure.
    expect(screen.getByTestId("from-trace-save")).toBeEnabled();
  });

  it("toggling the trace editor off hides it", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("from-trace-toggle"));
    expect(screen.getByTestId("from-trace-id")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("from-trace-toggle"));
    expect(screen.queryByTestId("from-trace-id")).not.toBeInTheDocument();
  });
});

describe("EvalDatasetDetail — editor validation branches", () => {
  it("rejects invalid Expected JSON without posting", async () => {
    let posted = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples`, () => {
        posted = true;
        return HttpResponse.json(envelope(example()), { status: 201 });
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-add-toggle"));
    const editor = await screen.findByTestId("example-editor");
    fireEvent.change(within(editor).getByTestId("example-input"), {
      target: { value: '{"input": "ok"}' },
    });
    fireEvent.change(within(editor).getByTestId("example-expected"), {
      target: { value: "[1, 2, 3]" },
    });
    await userEvent.click(within(editor).getByTestId("example-save"));
    expect(await within(editor).findByText(/Expected:.*JSON object/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("rejects a negative weight without posting", async () => {
    let posted = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples`, () => {
        posted = true;
        return HttpResponse.json(envelope(example()), { status: 201 });
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-add-toggle"));
    const editor = await screen.findByTestId("example-editor");
    fireEvent.change(within(editor).getByTestId("example-input"), {
      target: { value: '{"input": "ok"}' },
    });
    fireEvent.change(within(editor).getByTestId("example-expected"), {
      target: { value: '{"expected": "ok"}' },
    });
    // Exercise the weight onChange handler, then trip the >= 0 guard.
    fireEvent.change(within(editor).getByTestId("example-weight"), { target: { value: "-2" } });
    await userEvent.click(within(editor).getByTestId("example-save"));
    expect(await within(editor).findByText(/Weight must be a number/i)).toBeInTheDocument();
    expect(posted).toBe(false);
  });

  it("surfaces a server error from the editor save", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json({ detail: "duplicate example" }, { status: 409 }),
      ),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-add-toggle"));
    const editor = await screen.findByTestId("example-editor");
    fireEvent.change(within(editor).getByTestId("example-input"), {
      target: { value: '{"input": "ok"}' },
    });
    fireEvent.change(within(editor).getByTestId("example-expected"), {
      target: { value: '{"expected": "ok"}' },
    });
    await userEvent.click(within(editor).getByTestId("example-save"));
    expect(await within(editor).findByText("duplicate example")).toBeInTheDocument();
    // Save button is re-enabled after the failure.
    expect(within(editor).getByTestId("example-save")).toBeEnabled();
  });
});

describe("EvalDatasetDetail — sync, retire-error, and version filter", () => {
  it("syncs the dataset to MLflow (POST /sync) and refreshes", async () => {
    let synced = false;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/sync`, () => {
        synced = true;
        return HttpResponse.json(envelope(dataset({ mlflow_dataset_id: "d-99" })));
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("dataset-sync"));
    await waitFor(() => expect(synced).toBe(true));
  });

  it("surfaces a sync error in the action banner", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/sync`, () =>
        HttpResponse.json({ detail: "mlflow unreachable" }, { status: 502 }),
      ),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("dataset-sync"));
    expect(await screen.findByText("mlflow unreachable")).toBeInTheDocument();
  });

  it("surfaces a retire error in the action banner", async () => {
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, () =>
        HttpResponse.json(envelope([example()])),
      ),
      http.post(`${API_BASE}/eval-datasets/ED-1/examples/:exId/supersede`, () =>
        HttpResponse.json({ detail: "cannot retire" }, { status: 400 }),
      ),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    await userEvent.click(screen.getByTestId("example-retire"));
    expect(await screen.findByText("cannot retire")).toBeInTheDocument();
  });

  it("re-fetches with a version query when a version is selected", async () => {
    let lastVersion: string | null = null;
    server.use(
      http.get(`${API_BASE}/eval-datasets/ED-1`, () => HttpResponse.json(envelope(dataset()))),
      http.get(`${API_BASE}/eval-datasets/ED-1/examples`, ({ request }) => {
        lastVersion = new URL(request.url).searchParams.get("version");
        return HttpResponse.json(envelope([example()]));
      }),
    );
    renderPage();
    await screen.findAllByTestId("example-row");
    expect(lastVersion).toBeNull();
    await userEvent.selectOptions(screen.getByTestId("version-filter"), "1");
    await waitFor(() => expect(lastVersion).toBe("1"));
  });
});
