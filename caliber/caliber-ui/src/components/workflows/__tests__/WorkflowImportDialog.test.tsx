import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type {
  Workflow,
  WorkflowImportPreview,
  WorkflowVersion,
} from "@/api/workflowTypes";
import { caliberApi } from "@/api/caliberApi";
import { WorkflowImportDialog } from "@/components/workflows/WorkflowImportDialog";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

function sourceWorkflow(overrides: Partial<Workflow> = {}): Workflow {
  return {
    workflow_id: "WF-1",
    project_id: null,
    name: "Support Flow",
    description: "",
    owner: "@ops",
    status: "active",
    default_experiment_id: null,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

function version(overrides: Partial<WorkflowVersion> = {}): WorkflowVersion {
  return {
    version_id: "WFV-1",
    workflow_id: "WF-1",
    version_number: 1,
    status: "published",
    manifest: {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support Flow",
      nodes: {},
      edges: [],
    },
    manifest_hash: "hash-1",
    compiler_version: null,
    compiled_artifact_uri: null,
    compiled_bundle: null,
    validation_report: null,
    created_by: "@ops",
    created_at: "2026-06-01T00:00:00Z",
    published_by: "@ops",
    published_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

function preview(overrides: Partial<WorkflowImportPreview> = {}): WorkflowImportPreview {
  return {
    source_workflow_id: "WF-1",
    name: "Support Flow",
    description: "",
    node_count: 3,
    edge_count: 2,
    validation: { valid: true, errors: [], warnings: [] },
    dependencies: [],
    bundle_verification: null,
    ready_to_import: true,
    ...overrides,
  };
}

describe("WorkflowImportDialog (import mode)", () => {
  it("disables validation until a manifest is entered, and enables it once typed", async () => {
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    expect(screen.getByText("Import workflow or bundle")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-import-validate")).toBeDisabled();
    expect(screen.getByTestId("workflow-import-submit")).toBeDisabled();

    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    expect(screen.getByTestId("workflow-import-validate")).not.toBeDisabled();
  });

  it("validates a plain manifest_yaml payload and renders a ready-to-import preview", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(preview({ node_count: 4, edge_count: 3 })));
      }),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1\nname: Support Flow" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByText("Preflight passed")).toBeInTheDocument();
    expect(
      screen.getByText((_, node) => node?.textContent === "4 nodes · 3 edges · source ID WF-1"),
    ).toBeInTheDocument();
    expect(capturedBody).toEqual({
      manifest_yaml: "schema_version: 1\nname: Support Flow",
      name: undefined,
    });
  });

  it("sends a deployment_bundle payload when the pasted JSON is a caliber deployment bundle", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(preview()));
      }),
    );
    const bundle = {
      kind: "caliber.workflow_deployment_bundle",
      schema_version: 1,
      workflow: {},
    };
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: JSON.stringify(bundle) },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    await screen.findByTestId("workflow-import-preview");
    expect(capturedBody).toEqual({
      deployment_bundle: bundle,
      name: undefined,
    });
  });

  it("treats ordinary (non-bundle) JSON manifests as manifest_yaml rather than a deployment bundle", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(preview()));
      }),
    );
    const plainManifest = JSON.stringify({ schema_version: 1, name: "Plain" });
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: plainManifest },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    await screen.findByTestId("workflow-import-preview");
    expect(capturedBody).toEqual({ manifest_yaml: plainManifest, name: undefined });
  });

  it("includes the trimmed name override in the payload when provided", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, async ({ request }) => {
        capturedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(preview()));
      }),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-name"), {
      target: { value: "  Renamed Flow  " },
    });
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    await screen.findByTestId("workflow-import-preview");
    expect(capturedBody).toMatchObject({ name: "Renamed Flow" });
  });

  it("shows a needs-attention banner, blocking errors, and unresolved/resolved dependency rows", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(
          envelope(
            preview({
              ready_to_import: false,
              validation: {
                valid: false,
                errors: [
                  {
                    code: "missing_start",
                    path: "nodes.start",
                    message: "A start node is required.",
                    severity: "error",
                  },
                ],
                warnings: [],
              },
              dependencies: [
                {
                  kind: "tool",
                  reference: "lookup_policy",
                  path: "nodes.agent.tools[0]",
                  status: "resolved",
                  version: "1.0",
                  detail: "Found in registry.",
                },
                {
                  kind: "knowledge_base",
                  reference: "KB-1",
                  path: "nodes.rag.knowledge_base_id",
                  status: "unresolved",
                  version: null,
                  detail: "No matching knowledge base in this workspace.",
                },
                {
                  kind: "skill",
                  reference: "tone",
                  path: "nodes.agent.skills[0]",
                  status: "unverified",
                  version: null,
                  detail: "Skill snapshot not embedded.",
                },
              ],
            }),
          ),
        ),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByText("Preflight needs attention")).toBeInTheDocument();
    expect(screen.getByText("Blocking errors")).toBeInTheDocument();
    expect(screen.getByText("nodes.start")).toBeInTheDocument();
    expect(screen.getByText(/A start node is required\./)).toBeInTheDocument();
    expect(screen.getByText(/lookup_policy/)).toBeInTheDocument();
    expect(screen.getByText("resolved")).toBeInTheDocument();
    expect(screen.getByText("unresolved")).toBeInTheDocument();
    expect(screen.getByText("unverified")).toBeInTheDocument();
    // Not ready to import -> submit stays disabled even with a preview shown.
    expect(screen.getByTestId("workflow-import-submit")).toBeDisabled();
  });

  it("falls back to the literal 'manifest' label when a blocking error has no path", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(
          envelope(
            preview({
              ready_to_import: false,
              validation: {
                valid: false,
                errors: [
                  {
                    code: "root_invalid",
                    path: "",
                    message: "The manifest root is not an object.",
                    severity: "error",
                  },
                ],
                warnings: [],
              },
            }),
          ),
        ),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "not an object" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByText("manifest")).toBeInTheDocument();
    expect(
      screen.getByText(/The manifest root is not an object\./),
    ).toBeInTheDocument();
  });

  it("shows the no-dependencies message when the dependency list is empty", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview({ dependencies: [] }))),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(
      await screen.findByText("No external dependencies declared."),
    ).toBeInTheDocument();
  });

  it("renders verified and invalid bundle-integrity sections, including the digest", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(
          envelope(
            preview({
              bundle_verification: {
                valid: true,
                ready_to_deploy: true,
                dependency_count: 2,
                digest: "abc123",
                errors: [],
              },
            }),
          ),
        ),
      ),
    );
    const { rerender } = render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: JSON.stringify({ kind: "caliber.workflow_deployment_bundle" }) },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByText("verified")).toBeInTheDocument();
    expect(screen.getByText("2 locked", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("sha256:abc123")).toBeInTheDocument();

    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(
          envelope(
            preview({
              bundle_verification: {
                valid: false,
                ready_to_deploy: false,
                dependency_count: 0,
                digest: null,
                errors: ["digest mismatch"],
              },
            }),
          ),
        ),
      ),
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: JSON.stringify({ kind: "caliber.workflow_deployment_bundle", v: 2 }) },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    expect(await screen.findByText("invalid")).toBeInTheDocument();
    // No digest line rendered when digest is null.
    expect(screen.queryByText(/sha256:/)).not.toBeInTheDocument();
    rerender(<WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />);
  });

  it("shows a validating state while the request is in flight and re-enables afterward", async () => {
    let resolveRequest: (() => void) | undefined;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, async () => {
        await new Promise<void>((resolve) => {
          resolveRequest = resolve;
        });
        return HttpResponse.json(envelope(preview()));
      }),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(screen.getByText("Validating…")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-import-validate")).toBeDisabled();

    resolveRequest?.();
    await waitFor(() =>
      expect(screen.getByTestId("workflow-import-validate")).toHaveTextContent(
        "Validate dependencies",
      ),
    );
  });

  it("shows a friendly error and clears the preview when validation fails", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json({ detail: "manifest is not valid YAML" }, { status: 400 }),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "not: [valid" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "manifest is not valid YAML",
    );
    expect(screen.queryByTestId("workflow-import-preview")).not.toBeInTheDocument();
  });

  it("stringifies a non-Error rejection when validation throws a plain value", async () => {
    vi.spyOn(caliberApi, "previewWorkflowImport").mockRejectedValueOnce("boom");
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });

  it("stringifies a non-Error rejection when import throws a plain value", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview())),
      ),
    );
    vi.spyOn(caliberApi, "importWorkflow").mockRejectedValueOnce("import boom");
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");
    await userEvent.click(screen.getByTestId("workflow-import-submit"));

    expect(await screen.findByRole("alert")).toHaveTextContent("import boom");
  });

  it("clears a prior preview and error as soon as the manifest or name is edited again", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview())),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");

    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1\nname: changed" },
    });
    expect(screen.queryByTestId("workflow-import-preview")).not.toBeInTheDocument();
  });

  it("reads a chosen file into the manifest textarea and resets the preview", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview())),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    const manifestText = "schema_version: 1\nname: From File";
    const file = new File([manifestText], "manifest.yaml", { type: "text/yaml" });
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(fileInput, file);

    await waitFor(() =>
      expect(screen.getByTestId("workflow-import-manifest")).toHaveValue(manifestText),
    );
    expect(screen.queryByTestId("workflow-import-preview")).not.toBeInTheDocument();
  });

  it("ignores the file input event when no file is chosen", async () => {
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [] } });
    expect(screen.getByTestId("workflow-import-manifest")).toHaveValue("");
  });

  it("imports successfully and calls onImported, showing a Creating… submitting state", async () => {
    const onImported = vi.fn();
    let resolveImport: (() => void) | undefined;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview())),
      ),
      http.post(`${API_BASE}/workflows/import`, async () => {
        await new Promise<void>((resolve) => {
          resolveImport = resolve;
        });
        return HttpResponse.json(
          envelope({ workflow: sourceWorkflow(), version: version() }),
        );
      }),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={onImported} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");

    await userEvent.click(screen.getByTestId("workflow-import-submit"));
    expect(screen.getByText("Creating…")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-import-submit")).toBeDisabled();
    expect(screen.getByTestId("workflow-import-validate")).toBeDisabled();

    resolveImport?.();
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(onImported).toHaveBeenCalledWith({
      workflow: sourceWorkflow(),
      version: version(),
    });
  });

  it("does not submit when the preview isn't ready to import", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview({ ready_to_import: false }))),
      ),
    );
    const importSpy = vi.fn();
    server.use(http.post(`${API_BASE}/workflows/import`, importSpy));
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");
    expect(screen.getByTestId("workflow-import-submit")).toBeDisabled();
    expect(importSpy).not.toHaveBeenCalled();
  });

  it("shows an error when import fails after a successful preview", async () => {
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview())),
      ),
      http.post(`${API_BASE}/workflows/import`, () =>
        HttpResponse.json({ detail: "workflow name already exists" }, { status: 409 }),
      ),
    );
    render(
      <WorkflowImportDialog mode="import" onClose={vi.fn()} onImported={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");
    await userEvent.click(screen.getByTestId("workflow-import-submit"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "workflow name already exists",
    );
  });

  it("closes via Cancel, the close button, and a backdrop click, but not while importing", async () => {
    let resolveImport: (() => void) | undefined;
    server.use(
      http.post(`${API_BASE}/workflows/import/preview`, () =>
        HttpResponse.json(envelope(preview())),
      ),
      http.post(`${API_BASE}/workflows/import`, async () => {
        await new Promise<void>((resolve) => {
          resolveImport = resolve;
        });
        return HttpResponse.json(
          envelope({ workflow: sourceWorkflow(), version: version() }),
        );
      }),
    );
    const onClose = vi.fn();
    const { unmount } = render(
      <WorkflowImportDialog mode="import" onClose={onClose} onImported={vi.fn()} />,
    );

    await userEvent.click(screen.getByText("Cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByLabelText("Close import dialog"));
    expect(onClose).toHaveBeenCalledTimes(2);

    const backdrop = screen.getByRole("dialog").parentElement!;
    fireEvent.mouseDown(backdrop);
    expect(onClose).toHaveBeenCalledTimes(3);

    fireEvent.change(screen.getByTestId("workflow-import-manifest"), {
      target: { value: "schema_version: 1" },
    });
    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");
    await userEvent.click(screen.getByTestId("workflow-import-submit"));

    // Importing -> close affordances are disabled/no-ops.
    expect(screen.getByLabelText("Close import dialog")).toBeDisabled();
    await userEvent.click(screen.getByText("Cancel"));
    expect(onClose).toHaveBeenCalledTimes(3);

    resolveImport?.();
    await waitFor(() => expect(screen.queryByText("Creating…")).not.toBeInTheDocument());
    unmount();
  });
});

describe("WorkflowImportDialog (clone mode)", () => {
  it("prefills the name as '<source> Copy' and loads/sorts source versions newest-first", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () =>
        HttpResponse.json(
          envelope([
            version({ version_id: "v1", version_number: 1 }),
            version({ version_id: "v3", version_number: 3 }),
            version({ version_id: "v2", version_number: 2 }),
          ]),
        ),
      ),
    );
    render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow({ name: "Support Flow" })}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );
    expect(screen.getByText("Clone workflow as new")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-import-name")).toHaveValue("Support Flow Copy");
    expect(screen.getByTestId("workflow-clone-version")).toBeDisabled();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-clone-version")).not.toBeDisabled(),
    );
    const select = screen.getByTestId("workflow-clone-version") as HTMLSelectElement;
    expect(select.value).toBe("v3");
    expect(select).toHaveTextContent("v3 · published");
    expect(select).toHaveTextContent("v2 · published");
    expect(select).toHaveTextContent("v1 · published");
  });

  it("shows a warning when the source workflow has no versions to clone", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () =>
        HttpResponse.json(envelope([])),
      ),
    );
    render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow()}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );
    expect(
      await screen.findByText("This workflow has no saved version to clone."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workflow-import-validate")).toBeDisabled();
  });

  it("surfaces a load error when fetching source versions fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () =>
        HttpResponse.json({ detail: "workflow not found" }, { status: 404 }),
      ),
    );
    render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow()}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load source versions: workflow not found",
    );
  });

  it("requires a non-empty name once a version manifest is selected, and disables validation without one", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () =>
        HttpResponse.json(envelope([version()])),
      ),
    );
    render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow({ name: "Support Flow" })}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("workflow-clone-version")).not.toBeDisabled(),
    );
    // Name prefilled as "Support Flow Copy" and a version is loaded -> enabled.
    expect(screen.getByTestId("workflow-import-validate")).not.toBeDisabled();

    fireEvent.change(screen.getByTestId("workflow-import-name"), {
      target: { value: "   " },
    });
    expect(screen.getByTestId("workflow-import-validate")).toBeDisabled();

    fireEvent.change(screen.getByTestId("workflow-import-name"), {
      target: { value: "Cloned Flow" },
    });
    expect(screen.getByTestId("workflow-import-validate")).not.toBeDisabled();
  });

  it("validates using the selected version's manifest and clone name, and clones on submit", async () => {
    let capturedPreviewBody: Record<string, unknown> | null = null;
    let capturedImportBody: Record<string, unknown> | null = null;
    const onImported = vi.fn();
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () =>
        HttpResponse.json(
          envelope([
            version({ version_id: "v1", version_number: 1 }),
            version({ version_id: "v2", version_number: 2 }),
          ]),
        ),
      ),
      http.post(`${API_BASE}/workflows/import/preview`, async ({ request }) => {
        capturedPreviewBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(preview()));
      }),
      http.post(`${API_BASE}/workflows/import`, async ({ request }) => {
        capturedImportBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({ workflow: sourceWorkflow(), version: version() }),
        );
      }),
    );
    render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow({ name: "Support Flow" })}
        onClose={vi.fn()}
        onImported={onImported}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("workflow-clone-version")).not.toBeDisabled(),
    );
    await userEvent.selectOptions(screen.getByTestId("workflow-clone-version"), "v1");

    await userEvent.click(screen.getByTestId("workflow-import-validate"));
    await screen.findByTestId("workflow-import-preview");
    expect(capturedPreviewBody).toEqual({
      manifest: version({ version_id: "v1", version_number: 1 }).manifest,
      name: "Support Flow Copy",
    });

    await userEvent.click(screen.getByTestId("workflow-import-submit"));
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(capturedImportBody).toEqual({
      manifest: version({ version_id: "v1", version_number: 1 }).manifest,
      name: "Support Flow Copy",
    });
  });

  it("ignores a versions-load failure that resolves after the dialog has already unmounted", async () => {
    let rejectVersions: ((err: Error) => void) | undefined;
    const listSpy = vi
      .spyOn(caliberApi, "listWorkflowVersions")
      .mockReturnValue(
        new Promise((_, reject) => {
          rejectVersions = reject;
        }),
      );
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { unmount } = render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow()}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );
    unmount();
    rejectVersions?.(new Error("network down"));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The effect's cleanup flag should suppress the post-unmount setError,
    // so React never warns about updating state on an unmounted component.
    expect(errorSpy).not.toHaveBeenCalled();
    listSpy.mockRestore();
  });

  it("does not render the manifest textarea or file picker in clone mode", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/:workflowId/versions`, () =>
        HttpResponse.json(envelope([version()])),
      ),
    );
    render(
      <WorkflowImportDialog
        mode="clone"
        sourceWorkflow={sourceWorkflow()}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("workflow-import-manifest")).not.toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).not.toBeInTheDocument();
  });
});
