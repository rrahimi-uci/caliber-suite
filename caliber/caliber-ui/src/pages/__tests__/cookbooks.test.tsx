import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { CookbookCatalog, CookbookReadinessCheck } from "@/api/workflowTypes";
import { Cookbooks } from "@/pages/Cookbooks";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-08-04T00:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function recipe(id: string, prerequisites: string[] = []) {
  return {
    id,
    slug: `example-${id}`,
    title: `Example ${id}`,
    summary: `Summary for example ${id}`,
    icon: "📘",
    template_kind: "single_agent" as const,
    catalog_version: "2026.08",
    capabilities: [`capability-${id}`],
    prerequisites,
    // Kept in sync with the real payload by hand: tsconfig.app.json excludes
    // __tests__, and vitest does not typecheck, so a fixture missing a required
    // field is caught by neither.
    steps: [
      { id: `${id}.1`, title: `First step for ${id}`, route: "/prompts" },
      { id: `${id}.2`, title: `Second step for ${id}`, route: "/evaluations" },
    ],
    activation_requires_review: true,
    manifest_template: {} as CookbookCatalog["recipes"][number]["manifest_template"],
    readiness: {
      status: prerequisites.length > 0 ? "configuration_required" as const : "ready" as const,
      checks: prerequisites.map((label) => ({
        label,
        status: "operator_confirmation_required" as const,
      })),
    },
  };
}

const CATALOG: CookbookCatalog = {
  schema_version: 1,
  catalog_version: "2026.08",
  recipes: Array.from({ length: 16 }, (_, index) =>
    recipe(String(index + 1).padStart(2, "0"), index === 2 ? ["Enable approvals"] : []),
  ),
};

function Destination(): JSX.Element {
  const params = useParams();
  return <div>Editor {params.workflowId} {params.versionId}</div>;
}

function renderPage(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/cookbooks"]}
      >
        <Routes>
          <Route path="/cookbooks" element={<Cookbooks />} />
          <Route path="/workflows/:workflowId/editor/:versionId" element={<Destination />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Cookbooks", () => {
  it("renders all 16 built-in examples and filters by capability", async () => {
    server.use(
      http.get(`${API_BASE}/cookbooks`, () => HttpResponse.json(envelope(CATALOG))),
    );
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByTestId("cookbook-card-01")).toBeInTheDocument();
    expect(screen.getAllByTestId(/^cookbook-card-/)).toHaveLength(16);

    await user.type(screen.getByRole("searchbox", { name: "Search Cookbooks" }), "capability-16");
    expect(screen.getByTestId("cookbook-card-16")).toBeInTheDocument();
    expect(screen.queryByTestId("cookbook-card-01")).not.toBeInTheDocument();
  });

  it("requires prerequisite review, installs a paused draft, and opens its editor", async () => {
    let installBody: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API_BASE}/cookbooks`, () => HttpResponse.json(envelope(CATALOG))),
      http.post(`${API_BASE}/cookbooks/03/install`, async ({ request }) => {
        installBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json(envelope({
          recipe: CATALOG.recipes[2],
          workflow: {
            workflow_id: "WF-example",
            project_id: null,
            name: "Example install",
            description: "",
            owner: "@test",
            status: "paused",
            default_experiment_id: null,
            created_at: NOW,
            updated_at: NOW,
          },
          version: {
            version_id: "WFV-example",
            workflow_id: "WF-example",
            version_number: 1,
            status: "draft",
            manifest: {},
            manifest_hash: "a".repeat(64),
            compiler_version: null,
            compiled_artifact_uri: null,
            validation_report: null,
            compiled_bundle: null,
            created_by: "@test",
            created_at: NOW,
            published_by: null,
            published_at: null,
          },
          activation_requires_review: true,
        }), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId("install-cookbook-03"));
    const confirm = screen.getByTestId("confirm-cookbook-install");
    expect(confirm).toBeDisabled();
    await user.click(screen.getByTestId("cookbook-prerequisites-ack"));
    expect(confirm).toBeEnabled();
    await user.clear(screen.getByTestId("cookbook-install-name"));
    await user.type(screen.getByTestId("cookbook-install-name"), "Example install");
    await user.click(confirm);

    expect(await screen.findByText("Editor WF-example WFV-example")).toBeInTheDocument();
    await waitFor(() => expect(installBody).toEqual({
      name: "Example install",
      acknowledge_prerequisites: true,
    }));
  });

  /**
   * The server has always computed per-check readiness and shipped it with the
   * catalog; the page dropped it and rendered a bare "Setup needed" badge. These
   * pin the checks to the surface, because a badge that names no cause and
   * offers no route out is indistinguishable from a broken recipe.
   */
  describe("readiness checks", () => {
    function catalogWithChecks(checks: CookbookReadinessCheck[]): CookbookCatalog {
      const recipes = CATALOG.recipes.map((item) =>
        item.id === "01"
          ? { ...item, readiness: { status: "configuration_required" as const, checks } }
          : item,
      );
      return { ...CATALOG, recipes };
    }

    it("names each unmet check on the card and links the one with a fix", async () => {
      server.use(
        http.get(`${API_BASE}/cookbooks`, () =>
          HttpResponse.json(envelope(catalogWithChecks([
            {
              label: "Workflow queue, runtime approvals, and checkpoints",
              status: "configuration_required",
              settings_path: "/settings",
            },
            { label: "Configured model provider", status: "operator_confirmation_required" },
          ]))),
        ),
      );
      renderPage();

      const panel = await screen.findByTestId("cookbook-readiness-01");
      expect(panel).toHaveTextContent("Workflow queue, runtime approvals, and checkpoints");
      expect(panel).toHaveTextContent("Configured model provider");
      // Only the server-side capability check carries a route, so exactly one
      // "Configure" affordance should appear -- not one per check.
      const links = within(panel).getAllByRole("link", { name: "Configure" });
      expect(links).toHaveLength(1);
      expect(links[0]).toHaveAttribute("href", "/settings");
    });

    it("summarises the remainder rather than overflowing the card", async () => {
      server.use(
        http.get(`${API_BASE}/cookbooks`, () =>
          HttpResponse.json(envelope(catalogWithChecks([
            { label: "First check", status: "configuration_required" },
            { label: "Second check", status: "configuration_required" },
            { label: "Third check", status: "configuration_required" },
            { label: "Fourth check", status: "configuration_required" },
          ]))),
        ),
      );
      renderPage();

      const panel = await screen.findByTestId("cookbook-readiness-01");
      expect(panel).toHaveTextContent("First check");
      expect(panel).toHaveTextContent("Second check");
      expect(panel).not.toHaveTextContent("Third check");
      expect(panel).toHaveTextContent("+2 more before this is ready");
    });

    it("shows no readiness panel when every check passes", async () => {
      server.use(
        http.get(`${API_BASE}/cookbooks`, () =>
          HttpResponse.json(envelope(catalogWithChecks([
            { label: "Already satisfied", status: "ready" },
          ]))),
        ),
      );
      renderPage();

      await screen.findByTestId("cookbook-card-01");
      expect(screen.queryByTestId("cookbook-readiness-01")).not.toBeInTheDocument();
    });

    it("lists every unmet check in the install modal, not just the first two", async () => {
      server.use(
        http.get(`${API_BASE}/cookbooks`, () =>
          HttpResponse.json(envelope(catalogWithChecks([
            { label: "First check", status: "configuration_required" },
            { label: "Second check", status: "configuration_required" },
            { label: "Third check", status: "configuration_required" },
          ]))),
        ),
      );
      const user = userEvent.setup();
      renderPage();

      await user.click(await screen.findByTestId("install-cookbook-01"));
      const modal = screen.getByTestId("cookbook-install-readiness");
      expect(modal).toHaveTextContent("First check");
      expect(modal).toHaveTextContent("Second check");
      expect(modal).toHaveTextContent("Third check");
    });

    /**
     * Recipe 01 has no prerequisites, so the acknowledgement never renders and
     * the install button must not be gated on it. Regression guard for the
     * dead-end modal shape: unmet checks present, nothing to acknowledge.
     */
    it("keeps install enabled when unmet checks need no acknowledgement", async () => {
      server.use(
        http.get(`${API_BASE}/cookbooks`, () =>
          HttpResponse.json(envelope(catalogWithChecks([
            { label: "Server-side check", status: "configuration_required" },
          ]))),
        ),
      );
      const user = userEvent.setup();
      renderPage();

      await user.click(await screen.findByTestId("install-cookbook-01"));
      expect(screen.queryByTestId("cookbook-prerequisites-ack")).not.toBeInTheDocument();
      expect(screen.getByTestId("confirm-cookbook-install")).toBeEnabled();
    });
  });
});
