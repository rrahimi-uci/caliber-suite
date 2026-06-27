import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { RunFilePanel } from "@/components/workflows/RunFilePanel";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function file(overrides: Record<string, unknown> = {}) {
  return {
    file_id: "FILE-1",
    file_ref: "caliber://workflow-runs/WR-1/input/data.csv",
    name: "data.csv",
    kind: "input",
    relative_path: "data.csv",
    media_type: "text/csv",
    size_bytes: 2048,
    sha256: "abc",
    status: "attached",
    producer_node_id: null,
    created_at: "2026-06-05T00:00:00Z",
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("RunFilePanel", () => {
  it("lists files, switches tabs, and shows modern lineage cards", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: [
              file(),
              file({
                file_id: "FILE-2",
                file_ref: "caliber://workflow-runs/WR-1/artifact/report.pdf",
                name: "report.pdf",
                kind: "artifact",
                relative_path: "reports/report.pdf",
                media_type: "application/pdf",
                status: "artifact",
                producer_node_id: "support_agent",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ),
    );

    render(<RunFilePanel runId="WR-1" />);

    const inputCard = await screen.findByTestId("workflow-run-file-FILE-1");
    expect(inputCard).toHaveTextContent("data.csv");
    expect(screen.getByTestId("run-file-tab-input")).toHaveTextContent("Inputs (1)");
    expect(screen.getByTestId("run-file-tab-artifact")).toHaveTextContent("Artifacts (1)");

    await userEvent.click(screen.getByTestId("run-file-tab-artifact"));
    const artifactCard = await screen.findByTestId("workflow-run-file-FILE-2");
    expect(artifactCard).toHaveTextContent("report.pdf");
    expect(artifactCard).toHaveTextContent("support_agent");
    expect(screen.queryByTestId("workflow-run-file-FILE-1")).not.toBeInTheDocument();
  });

  it("provides a download link to the content proxy", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(envelope({ items: [file()], next_cursor: null })),
      ),
    );
    render(<RunFilePanel runId="WR-1" />);
    const card = await screen.findByTestId("workflow-run-file-FILE-1");
    const link = within(card).getByRole("link", { name: "Download" });
    expect(link).toHaveAttribute(
      "href",
      `${API_BASE}/workflow-runs/WR-1/files/FILE-1/content`,
    );
  });

  it("uploads a selected file then reloads the list", async () => {
    let uploaded = false;
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: uploaded ? [file({ name: "new.csv" })] : [],
            next_cursor: null,
          }),
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-1/files`, () => {
        uploaded = true;
        return HttpResponse.json(envelope(file({ name: "new.csv" })), {
          status: 201,
        });
      }),
    );

    render(<RunFilePanel runId="WR-1" />);
    expect(await screen.findByText("No input files.")).toBeInTheDocument();

    const input = screen.getByLabelText("Upload files");
    await userEvent.upload(
      input,
      new File(["a,b\n1,2\n"], "new.csv", { type: "text/csv" }),
    );

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-file-FILE-1")).toHaveTextContent(
        "new.csv",
      ),
    );
  });

  it("hides the uploader when canUpload is false", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(envelope({ items: [], next_cursor: null })),
      ),
    );
    render(<RunFilePanel runId="WR-1" canUpload={false} />);
    await screen.findByText("No input files.");
    expect(screen.queryByLabelText("Upload files")).not.toBeInTheDocument();
  });

  it("explains when a paused run has not written files yet", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(envelope({ items: [], next_cursor: null })),
      ),
    );

    render(<RunFilePanel runId="WR-1" runStatus="waiting_approval" canUpload={false} />);

    expect(await screen.findByText("No input files are available yet.")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "This run is paused at a resume gate before files of this kind were written.",
    );
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "Inspect recovery, checkpoints, or the debugger, then refresh after the run continues.",
    );
  });

  it("explains when a completed run never persisted files", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(envelope({ items: [], next_cursor: null })),
      ),
    );

    render(<RunFilePanel runId="WR-1" runStatus="completed" canUpload={false} />);

    expect(await screen.findByText("No input files were persisted for this run.")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "Inspect the debugger, final output, and other file tabs to confirm where the recorded result landed.",
    );
  });

  it("surfaces persisted object-store artifacts even when the run file registry is empty", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(envelope({ items: [], next_cursor: null })),
      ),
    );

    render(
      <RunFilePanel
        runId="WR-1"
        runStatus="completed"
        runSummary={{
          artifact_persistence: {
            status: "persisted",
            bucket: "caliber-suite",
            object_count: 3,
            artifact_names: ["kg.json", "report.html"],
          },
        }}
        canUpload={false}
      />,
    );

    expect(await screen.findByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "Run artifacts were written to object storage.",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "Bucket caliber-suite",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "3 objects",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "Named artifacts: kg.json, report.html",
    );

    await userEvent.click(screen.getByTestId("run-file-tab-artifact"));
    expect(
      await screen.findByText("No artifact files are indexed in the run file registry."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "This execution still reported object-store artifact persistence.",
    );
  });

  it("surfaces artifact upload failures instead of blaming the workflow for empty artifact tabs", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(envelope({ items: [], next_cursor: null })),
      ),
    );

    render(
      <RunFilePanel
        runId="WR-1"
        runStatus="completed"
        runSummary={{
          artifact_persistence: {
            status: "failed",
            bucket: "caliber-suite",
            object_count: 3,
            persisted_object_count: 1,
            artifact_names: ["kg.json"],
            recent_persisted_keys: ["pipeline/WR-1/kg.json"],
            failed_object_key: "pipeline/WR-1/report.html",
            error:
              "RuntimeError: object store offline while uploading pipeline/WR-1/report.html after storing 1 of 3 object(s)",
          },
        }}
        canUpload={false}
      />,
    );

    expect(await screen.findByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "Run artifact upload failed after execution completed.",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "RuntimeError: object store offline while uploading pipeline/WR-1/report.html after storing 1 of 3 object(s)",
    );
    expect(screen.getByTestId("workflow-run-file-persistence-status")).toHaveTextContent(
      "Failed",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "1 stored before failure",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "3 planned objects",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "Failing object: pipeline/WR-1/report.html",
    );
    expect(screen.getByTestId("workflow-run-file-persistence")).toHaveTextContent(
      "Stored before failure: pipeline/WR-1/kg.json",
    );

    await userEvent.click(screen.getByTestId("run-file-tab-artifact"));
    expect(
      await screen.findByText("No artifact files are indexed for this run."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "failed after 1 of 3 objects were stored",
    );
  });

  it("scopes files to the selected node and can return to all files", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: [
              file({
                file_id: "FILE-1",
                kind: "artifact",
                status: "artifact",
                name: "policy.json",
                file_ref: "caliber://workflow-runs/WR-1/artifact/policy.json",
                relative_path: "policy.json",
                producer_node_id: "support_agent",
              }),
              file({
                file_id: "FILE-2",
                kind: "artifact",
                status: "artifact",
                name: "report.json",
                file_ref: "caliber://workflow-runs/WR-1/artifact/report.json",
                relative_path: "report.json",
                producer_node_id: "reporter",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ),
    );

    render(<RunFilePanel runId="WR-1" selectedNodeId="support_agent" />);

    expect(await screen.findByTestId("workflow-run-file-scope")).toHaveTextContent("Focused on support_agent");
    expect(screen.getByTestId("run-file-tab-artifact")).toHaveTextContent("Artifacts (1)");
    expect(screen.getByTestId("workflow-run-file-FILE-1")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-file-FILE-2")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("workflow-run-file-scope-all"));
    expect(await screen.findByTestId("run-file-tab-artifact")).toHaveTextContent("Artifacts (2)");
    expect(screen.getByTestId("workflow-run-file-FILE-2")).toBeInTheDocument();
  });

  it("lets operators jump from a file back to its producing node", async () => {
    const onSelectNodeId = vi.fn();
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: [
              file({
                file_id: "FILE-9",
                kind: "artifact",
                status: "artifact",
                name: "trace.json",
                file_ref: "caliber://workflow-runs/WR-1/artifact/trace.json",
                relative_path: "trace.json",
                producer_node_id: "support_agent",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ),
    );

    render(<RunFilePanel runId="WR-1" onSelectNodeId={onSelectNodeId} />);
    const producer = await screen.findByTestId("workflow-run-file-producer-FILE-9");
    await userEvent.click(producer);
    expect(onSelectNodeId).toHaveBeenCalledWith("support_agent");
  });

  it("explains focused empty state when a selected step stopped before writing files", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: [
              file({
                file_id: "FILE-2",
                kind: "artifact",
                status: "artifact",
                name: "report.json",
                file_ref: "caliber://workflow-runs/WR-1/artifact/report.json",
                relative_path: "report.json",
                producer_node_id: "reporter",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ),
    );

    render(
      <RunFilePanel
        runId="WR-1"
        runStatus="failed"
        selectedNodeId="support_agent"
        canUpload={false}
      />,
    );

    expect(await screen.findByText("No input files are linked to support_agent.")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "This step did not persist files of this kind before the run stopped.",
    );
    expect(screen.getByTestId("workflow-run-file-panel")).toHaveTextContent(
      "Inspect the debugger and recovery panels, or clear the step focus to inspect earlier artifacts.",
    );
  });

  it("surfaces an API error", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    render(<RunFilePanel runId="WR-1" runStatus="failed" />);
    expect(await screen.findByTestId("workflow-run-file-load-error")).toHaveTextContent(
      "Files and artifact lineage could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-file-load-error")).toHaveTextContent(
      "Inspect the debugger, recovery diagnostics, and retry lineage",
    );
    expect(screen.getByTestId("workflow-run-file-load-error")).toHaveTextContent(
      "Latest file error: boom",
    );
  });

  it("keeps existing lineage visible when an upload fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json(
          envelope({
            items: [
              file({
                file_id: "FILE-EXISTING",
                name: "existing.csv",
              }),
            ],
            next_cursor: null,
          }),
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-1/files`, () =>
        HttpResponse.json({ detail: "object store offline" }, { status: 503 }),
      ),
    );

    render(<RunFilePanel runId="WR-1" />);
    expect(await screen.findByTestId("workflow-run-file-FILE-EXISTING")).toHaveTextContent(
      "existing.csv",
    );

    const input = screen.getByLabelText("Upload files");
    await userEvent.upload(
      input,
      new File(["a,b\n1,2\n"], "new.csv", { type: "text/csv" }),
    );

    expect(await screen.findByTestId("workflow-run-file-upload-error")).toHaveTextContent(
      "Uploading files to this run failed.",
    );
    expect(screen.getByTestId("workflow-run-file-upload-error")).toHaveTextContent(
      "Any files that were already persisted stay visible below",
    );
    expect(screen.getByTestId("workflow-run-file-upload-error")).toHaveTextContent(
      "Latest upload error: object store offline",
    );
    expect(screen.getByTestId("workflow-run-file-FILE-EXISTING")).toHaveTextContent(
      "existing.csv",
    );
  });
});
