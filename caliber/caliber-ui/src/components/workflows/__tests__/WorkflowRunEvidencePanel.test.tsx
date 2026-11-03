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

import { WorkflowRunEvidencePanel } from "@/components/workflows/WorkflowRunEvidencePanel";
import { render, screen, userEvent, waitFor } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function report(matches_original: boolean) {
  return {
    schema_version: "v1",
    workflow_run_id: "WR-1",
    manifest_id: "MANIFEST-1",
    manifest_digest: "sha256:manifest",
    observation_count: 2,
    observation_ids: ["OBS-1", "OBS-2"],
    criteria_version: "v1",
    decisions: [],
    matches_original,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("WorkflowRunEvidencePanel", () => {
  it("downloads the authenticated evidence archive successfully", async () => {
    server.use(
      http.get(
        `${API_BASE}/workflow-runs/WR-1/evidence`,
        () =>
          new HttpResponse(
            new TextEncoder().encode("zip-bytes"),
            {
              status: 200,
              headers: { "Content-Type": "application/zip" },
            },
          ),
      ),
    );
    const createObjectUrl = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:evidence");
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL");
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click");

    render(<WorkflowRunEvidencePanel runId="WR-1" />);
    await userEvent.click(
      screen.getByRole("button", { name: "Download evidence archive" }),
    );

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Evidence archive downloaded.",
    );
    expect(createObjectUrl).toHaveBeenCalledOnce();
    expect(anchorClick).toHaveBeenCalledOnce();
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:evidence");
  });

  it("shows the canonical matches_original=true replay result", async () => {
    server.use(
      http.post(`${API_BASE}/workflow-runs/WR-1/replay`, () =>
        HttpResponse.json(envelope(report(true))),
      ),
    );

    render(<WorkflowRunEvidencePanel runId="WR-1" />);
    await userEvent.click(
      screen.getByRole("button", { name: "Replay historical evidence" }),
    );

    const result = await screen.findByTestId("workflow-run-replay-result");
    expect(result).toHaveTextContent("matches_original: true");
    expect(result).toHaveTextContent("matches the original run");
  });

  it("surfaces a non-matching historical replay without relabeling it as success", async () => {
    server.use(
      http.post(`${API_BASE}/workflow-runs/WR-1/replay`, () =>
        HttpResponse.json(envelope(report(false))),
      ),
    );

    render(<WorkflowRunEvidencePanel runId="WR-1" />);
    await userEvent.click(
      screen.getByRole("button", { name: "Replay historical evidence" }),
    );

    const result = await screen.findByTestId("workflow-run-replay-result");
    expect(result).toHaveTextContent("matches_original: false");
    expect(result).toHaveTextContent("does not match the original run");
    expect(result).toHaveAttribute("role", "status");
  });

  it("shows a clear refusal/error state when replay is rejected", async () => {
    server.use(
      http.post(`${API_BASE}/workflow-runs/WR-1/replay`, () =>
        HttpResponse.json(
          { detail: "historical replay refused by policy" },
          { status: 403 },
        ),
      ),
    );

    render(<WorkflowRunEvidencePanel runId="WR-1" />);
    await userEvent.click(
      screen.getByRole("button", { name: "Replay historical evidence" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-replay-error")).toHaveTextContent(
        "Historical replay refused or failed: historical replay refused by policy",
      ),
    );
    expect(screen.getByTestId("workflow-run-replay-error")).toHaveAttribute(
      "role",
      "alert",
    );
  });
});
