import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

import { ToolCalibrationJobs } from "@/components/ToolCalibrationJobs";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function renderPanel(canOperate = true): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <ToolCalibrationJobs toolId="TL-1" canOperate={canOperate} />
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("ToolCalibrationJobs", () => {
  it("requires an operator reason and retries an ambiguous run as a new job", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/tools/TL-1/calibration-jobs`, () =>
        HttpResponse.json({
          data: {
            jobs: [
              {
                job_id: "CAL-running",
                status: "running",
                requested_by: "@operator",
                created_at: "2026-08-04T00:00:00Z",
                claimed_at: "2026-08-04T00:00:01Z",
                claimed_by: "worker:1",
                finished_at: null,
                retry_of_job_id: null,
                resolution: null,
                resolution_reason: null,
                resolved_by: null,
                resolved_at: null,
              },
            ],
            total: 1,
          },
        }),
      ),
      http.post(
        `${API_BASE}/tools/TL-1/calibration-jobs/CAL-running/resolve`,
        async ({ request }) => {
          bodies.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json({
            data: {
              job_id: "CAL-running",
              status: "failed",
              resolution: "retry",
              retry_job_id: "CAL-retry",
            },
          });
        },
      ),
    );
    vi.spyOn(window, "prompt").mockReturnValue("worker process disappeared");

    renderPanel();
    expect(
      await screen.findByTestId("tool-calibration-job-CAL-running"),
    ).toHaveTextContent("claimed by worker:1");
    fireEvent.click(screen.getByRole("button", { name: "Retry as new job" }));

    await waitFor(() =>
      expect(bodies).toEqual([
        { action: "retry", reason: "worker process disappeared" },
      ]),
    );
  });

  it("shows history without privileged actions to a viewer", async () => {
    server.use(
      http.get(`${API_BASE}/tools/TL-1/calibration-jobs`, () =>
        HttpResponse.json({
          data: {
            jobs: [{ job_id: "CAL-running", status: "running" }],
            total: 1,
          },
        }),
      ),
    );

    renderPanel(false);

    expect(
      await screen.findByTestId("tool-calibration-job-CAL-running"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Queue calibration" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Retry as new job" }),
    ).not.toBeInTheDocument();
  });
});
