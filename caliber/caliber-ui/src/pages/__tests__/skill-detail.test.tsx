import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { SkillDetail } from "@/pages/SkillDetail";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeSkill(overrides: Record<string, unknown> = {}) {
  return {
    skill_id: "SK-1",
    name: "tool-grounding",
    description: "Ground claims in a tool call.",
    summary: "Call the tool before asserting a fact.",
    content: "When the user asks about a policy, call the tool first.",
    owner: "@caliber",
    category: "workflow_automation",
    tags: ["tool-use", "grounding"],
    skill_metadata: {},
    allowed_tools: null,
    depends_on: [],
    status: "active",
    version: 3,
    created_at: "2026-05-30T00:00:00Z",
    updated_at: "2026-05-30T00:00:00Z",
    ...overrides,
  };
}

function makeSkillPackage(overrides: Record<string, unknown> = {}) {
  return {
    root: "tool-grounding",
    format: "openai-skill",
    files: [
      {
        path: "tool-grounding/SKILL.md",
        kind: "skill",
        content:
          "---\nname: tool-grounding\ndescription: Ground claims in a tool call.\n---\n\nCall the tool.",
        size_bytes: 91,
      },
      {
        path: "tool-grounding/agents/openai.yaml",
        kind: "agent-metadata",
        content:
          'interface:\n  display_name: "Tool Grounding"\n  default_prompt: "Use $tool-grounding."\n',
        size_bytes: 87,
      },
      {
        path: "tool-grounding/references/checklist.md",
        kind: "reference",
        content: "Check the tool result.",
        size_bytes: 22,
      },
    ],
    resource_counts: { scripts: 0, references: 1, assets: 0 },
    warnings: [],
    is_valid: true,
    ...overrides,
  };
}

function renderDetail(
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  }),
): void {
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/skills/SK-1"]}
      >
        <Routes>
          <Route path="/skills/:skillId" element={<SkillDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("SkillDetail", () => {
  it("shows a recoverable error instead of loading forever when the skill request fails", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json({ detail: "skill not found" }, { status: 404 }),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(
          { detail: "skill package not found" },
          { status: 404 },
        ),
      ),
    );

    renderDetail();

    expect(await screen.findByTestId("skill-detail-error")).toHaveTextContent(
      "skill not found",
    );
    expect(screen.queryByText("Loading skill…")).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to skills" }),
    ).toHaveAttribute("href", "/skills");
  });

  it("keeps cached skill content visible when a background refresh fails", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    qc.setQueryData(["skill", "SK-1"], makeSkill());
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(
          { detail: "skill refresh unavailable" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
    );

    renderDetail(qc);

    expect(
      await screen.findByRole("heading", { name: /tool-grounding/ }),
    ).toBeInTheDocument();
    expect(
      await screen.findByTestId("skill-detail-refresh-warning"),
    ).toHaveTextContent("skill refresh unavailable");
    expect(screen.queryByTestId("skill-detail-error")).not.toBeInTheDocument();
  });

  it("renders the version history panel on the Versions tab", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.get(`${API_BASE}/skills/SK-1/versions`, () =>
        HttpResponse.json(
          envelope([
            {
              skill_version_id: "SKV-2",
              skill_id: "SK-1",
              version_number: 2,
              content: "v2",
              summary: "s",
              created_by: "@a",
              created_at: null,
            },
            {
              skill_version_id: "SKV-1",
              skill_id: "SK-1",
              version_number: 1,
              content: "v1",
              summary: "s",
              created_by: "@a",
              created_at: null,
            },
          ]),
        ),
      ),
    );
    renderDetail();
    await screen.findByRole("heading", { name: "tool-grounding" });
    await userEvent.click(screen.getByRole("button", { name: "Versions" }));
    expect(await screen.findByTestId("version-panel")).toBeInTheDocument();
    expect(screen.getByTestId("version-row-2")).toBeInTheDocument();
    expect(screen.getByTestId("version-row-1")).toBeInTheDocument();
  });

  it("shows the skill content and version", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
    );
    renderDetail();
    expect(
      await screen.findByRole("heading", { name: "tool-grounding" }),
    ).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    // Overview is the default tab — the always-loaded summary (level 1) is shown there.
    expect(
      screen.getByText(/call the tool before asserting a fact/i),
    ).toBeInTheDocument();
    // The full content (level 2) lives behind the Content tab.
    await userEvent.click(screen.getByRole("button", { name: "Content" }));
    expect(screen.getByText(/call the tool first/i)).toBeInTheDocument();
  });

  it("switches between the Overview and Content tabs", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
    );
    renderDetail();

    // Default Overview tab: summary + package panel; no raw content yet.
    expect(await screen.findByTestId("skill-summary")).toBeInTheDocument();
    expect(screen.getByTestId("skill-package-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-content")).not.toBeInTheDocument();

    // Switch to Content: raw source pre is shown, package panel is gone.
    await userEvent.click(screen.getByRole("button", { name: "Content" }));
    expect(await screen.findByTestId("skill-content")).toBeInTheDocument();
    expect(screen.getByText(/call the tool first/i)).toBeInTheDocument();
    expect(screen.queryByTestId("skill-package-panel")).not.toBeInTheDocument();

    // Back to Overview.
    await userEvent.click(screen.getByRole("button", { name: "Overview" }));
    expect(
      await screen.findByTestId("skill-package-panel"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("skill-content")).not.toBeInTheDocument();
  });

  it("renders the right-rail metadata, composition, and skill_metadata JSON", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(
          envelope(
            makeSkill({
              depends_on: ["retrieval"],
              allowed_tools: "lookup_policy",
              skill_metadata: { selection_threshold: 0.55 },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
    );
    renderDetail();

    // Composition: depends_on tile + allowed_tools pill.
    expect(await screen.findByText("retrieval")).toBeInTheDocument();
    expect(screen.getByText("lookup_policy")).toBeInTheDocument();

    // skill_metadata is rendered as pretty-printed JSON.
    const metadata = screen.getByTestId("skill-metadata");
    expect(metadata).toHaveTextContent('"selection_threshold": 0.55');
  });

  it("edits content and reports the version bump", async () => {
    const captured: { body: Record<string, unknown> | null } = { body: null };
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(
          envelope(makeSkill({ version: captured.body ? 4 : 3 })),
        ),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        captured.body = body;
        return HttpResponse.json(
          envelope(makeSkill({ version: 4, content: String(body.content) })),
        );
      }),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-edit-btn"));
    const content = await screen.findByTestId("skill-content");
    await userEvent.clear(content);
    await userEvent.type(content, "New grounded instructions.");
    await userEvent.click(screen.getByTestId("skill-save"));

    await waitFor(() => expect(captured.body).not.toBeNull());
    expect(captured.body?.content).toBe("New grounded instructions.");
    expect(await screen.findByText(/now v4/i)).toBeInTheDocument();
  });

  it("edits owner and metadata JSON", async () => {
    const captured: { body: Record<string, unknown> | null } = { body: null };
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(
          envelope(
            makeSkill({
              owner: "@caliber",
              skill_metadata: { openai_package: { resources: [] } },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        captured.body = body;
        return HttpResponse.json(
          envelope(
            makeSkill({
              owner: String(body.owner),
              skill_metadata: body.skill_metadata as Record<string, unknown>,
            }),
          ),
        );
      }),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-edit-btn"));
    const owner = await screen.findByTestId("skill-owner");
    await userEvent.clear(owner);
    await userEvent.type(owner, "@platform");
    const metadata = screen.getByTestId("skill-metadata");
    fireEvent.change(metadata, {
      target: {
        value: '{"openai_package":{"resources":[]},"reviewed_by":"@qa"}',
      },
    });
    await userEvent.click(screen.getByTestId("skill-save"));

    await waitFor(() => expect(captured.body).not.toBeNull());
    expect(captured.body?.owner).toBe("@platform");
    expect(captured.body?.skill_metadata).toEqual({
      openai_package: { resources: [] },
      reviewed_by: "@qa",
    });
  });

  it("edits summary, description, category, lists, allowed tools, and status", async () => {
    const captured: { body: Record<string, unknown> | null } = { body: null };
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        captured.body = body;
        return HttpResponse.json(envelope(makeSkill({ ...body, version: 3 })));
      }),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-edit-btn"));
    fireEvent.change(await screen.findByTestId("skill-summary"), {
      target: { value: "Use tools before making policy claims." },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Updated operational grounding skill." },
    });
    await userEvent.selectOptions(
      screen.getByLabelText("Category"),
      "research",
    );
    await userEvent.selectOptions(screen.getByLabelText("Status"), "archived");
    fireEvent.change(screen.getByLabelText("Tags (comma-separated)"), {
      target: { value: " grounding, policy , research " },
    });
    fireEvent.change(screen.getByLabelText("Depends on (comma-separated)"), {
      target: { value: "retrieval, verification" },
    });
    fireEvent.change(screen.getByLabelText("Allowed tools"), {
      target: { value: "lookup_policy" },
    });
    await userEvent.click(screen.getByTestId("skill-save"));

    await waitFor(() => expect(captured.body).not.toBeNull());
    expect(captured.body).toMatchObject({
      summary: "Use tools before making policy claims.",
      description: "Updated operational grounding skill.",
      category: "research",
      tags: ["grounding", "policy", "research"],
      depends_on: ["retrieval", "verification"],
      allowed_tools: "lookup_policy",
      status: "archived",
    });
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("archives and restores a skill from the detail toolbar", async () => {
    let current = makeSkill();
    const patches: string[] = [];
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(current)),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, async ({ request }) => {
        const body = (await request.json()) as { status?: string };
        patches.push(String(body.status));
        current = makeSkill({ status: body.status });
        return HttpResponse.json(envelope(current));
      }),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-status-btn"));
    await waitFor(() => expect(patches).toEqual(["archived"]));
    expect(await screen.findByText("Skill archived.")).toBeInTheDocument();
    expect(await screen.findByText(/This skill is/i)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("skill-status-btn"));
    await waitFor(() => expect(patches).toEqual(["archived", "active"]));
    expect(await screen.findByText("Skill restored.")).toBeInTheDocument();
  });

  it("blocks saving invalid metadata JSON", async () => {
    let patchCalled = false;
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, () => {
        patchCalled = true;
        return HttpResponse.json(envelope(makeSkill()));
      }),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-edit-btn"));
    const metadata = await screen.findByTestId("skill-metadata");
    fireEvent.change(metadata, { target: { value: "{bad json" } });
    await userEvent.click(screen.getByTestId("skill-save"));

    expect(
      await screen.findByText(/Metadata JSON is invalid/i),
    ).toBeInTheDocument();
    expect(patchCalled).toBe(false);
  });

  it("blocks metadata values that are valid JSON but not an object", async () => {
    let patchCalled = false;
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, () => {
        patchCalled = true;
        return HttpResponse.json(envelope(makeSkill()));
      }),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-edit-btn"));
    fireEvent.change(await screen.findByTestId("skill-metadata"), {
      target: { value: "[]" },
    });
    await userEvent.click(screen.getByTestId("skill-save"));

    expect(
      await screen.findByText("Metadata must be a JSON object."),
    ).toBeInTheDocument();
    expect(patchCalled).toBe(false);
  });

  it("shows save failures and lets cancel clear local form errors", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.patch(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json({ detail: "write denied" }, { status: 500 }),
      ),
    );
    renderDetail();

    await userEvent.click(await screen.findByTestId("skill-edit-btn"));
    fireEvent.change(await screen.findByTestId("skill-metadata"), {
      target: { value: "[]" },
    });
    await userEvent.click(screen.getByTestId("skill-save"));
    expect(
      await screen.findByText("Metadata must be a JSON object."),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByTestId("skill-edit-btn"));
    expect(
      screen.queryByText("Metadata must be a JSON object."),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId("skill-metadata"), {
      target: { value: "{}" },
    });
    await userEvent.click(screen.getByTestId("skill-save"));
    expect(
      await screen.findByText(/Save failed: write denied/i),
    ).toBeInTheDocument();
  });

  it("shows the OpenAI package preview and download link", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
    );
    renderDetail();

    expect(
      await screen.findByTestId("skill-package-panel"),
    ).toBeInTheDocument();
    expect(screen.getByText("tool-grounding/SKILL.md")).toBeInTheDocument();
    expect(
      screen.getByText("tool-grounding/agents/openai.yaml"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("tool-grounding/references/checklist.md"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Use \$tool-grounding/)).toBeInTheDocument();
    expect(screen.getByTestId("skill-package-download")).toHaveAttribute(
      "href",
      `${API_BASE}/skills/SK-1/package.zip`,
    );
  });

  it("shows package warnings and preview errors without hiding the download link", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(
          envelope(
            makeSkill({ tags: [], depends_on: [], allowed_tools: null }),
          ),
        ),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(
          { detail: "package builder unavailable" },
          { status: 500 },
        ),
      ),
    );
    renderDetail();

    expect(
      await screen.findByTestId("skill-package-panel"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(3);
    expect(
      await screen.findByText(
        /Package preview failed: package builder unavailable/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("skill-package-download")).toHaveAttribute(
      "href",
      `${API_BASE}/skills/SK-1/package.zip`,
    );
  });

  it("renders package warnings when the generated package is incomplete", async () => {
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(
          envelope(
            makeSkillPackage({
              files: [],
              resource_counts: { scripts: 1, references: 0, assets: 2 },
              warnings: ["Missing SKILL.md.", "Missing agent metadata."],
              is_valid: false,
            }),
          ),
        ),
      ),
    );
    renderDetail();

    expect(
      await screen.findByText(/Missing SKILL.md. Missing agent metadata./i),
    ).toBeInTheDocument();
    expect(screen.getByText("Scripts")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("SKILL.md")).not.toBeInTheDocument();
  });

  it("imports a skill package and navigates to the new skill", async () => {
    let imported: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.post(`${API_BASE}/skills/import-package`, async ({ request }) => {
        imported = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(makeSkill({ skill_id: "SK-2", name: "imported-skill" })),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/skills/SK-2`, () =>
        HttpResponse.json(
          envelope(makeSkill({ skill_id: "SK-2", name: "imported-skill" })),
        ),
      ),
      http.get(`${API_BASE}/skills/SK-2/package`, () =>
        HttpResponse.json(
          envelope(makeSkillPackage({ root: "imported-skill" })),
        ),
      ),
    );
    renderDetail();
    await screen.findByRole("heading", { name: "tool-grounding" });

    const file = new File(
      ["---\nname: imported-skill\ndescription: x\n---\n\nDo the thing."],
      "SKILL.md",
      { type: "text/markdown" },
    );
    const input = screen.getByLabelText(
      "Import skill package folder",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });

    // The selected file is sent as a {path, content} record with the importer's owner,
    // then the page navigates to the newly created skill.
    await waitFor(() => expect(imported).not.toBeNull());
    const body = imported as {
      owner: string;
      files: Array<{ path: string; content: string }>;
    };
    expect(body.owner).toBe("@local-admin");
    expect(body.files).toHaveLength(1);
    expect(body.files[0]!.path).toBe("SKILL.md");
    expect(body.files[0]!.content).toContain("name: imported-skill");
    expect(
      await screen.findByRole("heading", { name: "imported-skill" }),
    ).toBeInTheDocument();
  });

  it("uploads a ZIP with an explicit conflict strategy", async () => {
    let uploaded: { fileName: string; strategy: string; renameTo: string } | null =
      null;
    server.use(
      http.get(`${API_BASE}/skills/SK-1`, () =>
        HttpResponse.json(envelope(makeSkill())),
      ),
      http.get(`${API_BASE}/skills/SK-1/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage())),
      ),
      http.post(`${API_BASE}/skills/import-package.zip`, async ({ request }) => {
        const form = await request.formData();
        const file = form.get("file") as File;
        uploaded = {
          fileName: file.name,
          strategy: String(form.get("conflict_strategy")),
          renameTo: String(form.get("rename_to")),
        };
        return HttpResponse.json(
          envelope(makeSkill({ skill_id: "SK-3", name: "renamed-skill" })),
          { status: 201 },
        );
      }),
      http.get(`${API_BASE}/skills/SK-3`, () =>
        HttpResponse.json(
          envelope(makeSkill({ skill_id: "SK-3", name: "renamed-skill" })),
        ),
      ),
      http.get(`${API_BASE}/skills/SK-3/package`, () =>
        HttpResponse.json(envelope(makeSkillPackage({ root: "renamed-skill" }))),
      ),
    );
    renderDetail();
    await screen.findByRole("heading", { name: "tool-grounding" });

    fireEvent.change(screen.getByLabelText("ZIP conflict strategy"), {
      target: { value: "rename" },
    });
    fireEvent.change(screen.getByLabelText("Renamed skill name"), {
      target: { value: "renamed-skill" },
    });
    const archive = new File(["zip-bytes"], "portable.zip", {
      type: "application/zip",
    });
    fireEvent.change(screen.getByLabelText("Import skill package ZIP"), {
      target: { files: [archive] },
    });

    await waitFor(() => expect(uploaded).not.toBeNull());
    expect(uploaded).toMatchObject({
      strategy: "rename",
      renameTo: "renamed-skill",
    });
    // happy-dom currently normalizes multipart File names to "blob"; browsers
    // retain the explicit filename supplied by the API client.
    expect(["portable.zip", "blob"]).toContain(uploaded?.fileName);
    expect(
      await screen.findByRole("heading", { name: "renamed-skill" }),
    ).toBeInTheDocument();
  });
});
