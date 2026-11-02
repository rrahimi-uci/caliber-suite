/**
 * The sidebar renders the server's per-adapter support (B15-Support.3, #771).
 *
 * The named cases are the issue's oracle: ``supported_visible``, ``preview_label_retained``,
 * ``unavailable_disabled`` and ``alpha_navigation_regression``. Everything is read from
 * ``GET /capabilities`` ``adapters`` (served here as the server sends it, never derived from the
 * UI's own constants), and the property under test throughout is the fail-closed one: nothing
 * reads as supported unless the server said so, completely.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { AdapterCapability, AdapterId } from "@/api/workflowTypes";
import { Sidebar } from "@/components/Sidebar";
import { capabilitiesBody } from "@/test/handlers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

/** The exact texts ``caliber/features.py`` sends (docs/operate/reference-profile.md, "Refusals and recovery"). */
const MESSAGES = {
  releaseSupported:
    "Workspace release evaluation is supported: the evaluator and its evidence shape match the scenario evidence.",
  connectorSupported:
    "OpenAPI document-request connector is supported: the live transport matches its admission record.",
  mcpPreview:
    "MCP servers are preview: no scenario evidence supports them in the reference profile, and no setting changes that.",
  openapiPreview:
    "OpenAPI integrations are preview: no scenario evidence supports them in the reference profile, and no setting changes that.",
  mcpUnavailable:
    "MCP servers are unavailable under the reference profile: it lists no MCP command or host, so no server is reachable.",
  evaluatorDisabled:
    "Workspace release evaluation is unavailable in this deployment. Set CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED=true to enable it.",
  integrationsOff:
    "MCP servers and OpenAPI integrations is disabled in this deployment. Set CALIBER_FEATURE_INTEGRATIONS=true to enable it.",
  connectorPreview:
    "OpenAPI document-request connector is preview in this deployment: it is supported only under CALIBER_PROFILE=reference. Set CALIBER_PROFILE=reference and start from a configuration that passes the profile to use it as a supported path.",
} as const;

const KIND: Record<AdapterId, AdapterCapability["kind"]> = {
  document_request_connector: "connector",
  release_evaluation: "release_evaluation",
  mcp_servers: "mcp_server",
  openapi_integrations: "openapi_integration",
};

const LABEL: Record<AdapterId, string> = {
  document_request_connector: "OpenAPI document-request connector",
  release_evaluation: "Workspace release evaluation",
  mcp_servers: "MCP servers",
  openapi_integrations: "OpenAPI integrations",
};

function entry(
  id: AdapterId,
  status: AdapterCapability["status"],
  message: string,
  extra: Partial<AdapterCapability> = {},
): AdapterCapability {
  return {
    status,
    kind: KIND[id],
    label: LABEL[id],
    reason_code: status === "supported" ? null : "no_support_evidence",
    disabled_by: status === "supported" ? null : "profile",
    message,
    evidence:
      status === "supported"
        ? ["tests/egi_scenarios/x.py::test_one", "tests/egi_scenarios/x.py::test_two"]
        : [],
    ...extra,
  };
}

/** A reference deployment: the reference connector and release evaluation supported, MCP and OpenAPI preview. */
function referenceAdapters(): Partial<Record<AdapterId, AdapterCapability>> {
  return {
    document_request_connector: entry(
      "document_request_connector",
      "supported",
      MESSAGES.connectorSupported,
    ),
    release_evaluation: entry("release_evaluation", "supported", MESSAGES.releaseSupported),
    mcp_servers: entry("mcp_servers", "preview", MESSAGES.mcpPreview),
    openapi_integrations: entry("openapi_integrations", "preview", MESSAGES.openapiPreview),
  };
}

const ALL_ON = {
  aria: true,
  integrations: true,
  cookbooks: true,
  release_candidates: true,
} as const;

function serve(
  enabled: Parameters<typeof capabilitiesBody>[0],
  adapters?: Parameters<typeof capabilitiesBody>[1],
): void {
  server.use(
    http.get(`${API_BASE}/capabilities`, () =>
      HttpResponse.json(capabilitiesBody(enabled, adapters)),
    ),
    http.get(`${API_BASE}/aria/plans`, () => HttpResponse.json({ data: [] })),
  );
}

function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.hash}`}</output>;
}

function renderSidebar(initialPath = "/", collapsed = false): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[initialPath]}
      >
        <Sidebar health="ok" collapsed={collapsed} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openAllGroups(): Promise<HTMLElement> {
  await screen.findByTestId("nav-group-toggle-preview");
  for (const toggle of document.querySelectorAll<HTMLElement>('[data-testid^="nav-group-toggle-"]')) {
    if (toggle.getAttribute("aria-expanded") === "false") await userEvent.click(toggle);
  }
  return screen.getByLabelText("CALIBER navigation");
}

/** The chip inside the item whose label is ``label``. */
function chipOf(nav: HTMLElement, label: string): HTMLElement | null {
  const item = within(nav).getByText(label).closest(".nav-item") as HTMLElement;
  return within(item).queryByTestId("adapter-support-chip");
}

/** The sr-only text an item is described by. */
function descriptionOf(item: HTMLElement): string {
  const id = item.getAttribute("aria-describedby");
  return id ? (document.getElementById(id)?.textContent ?? "") : "";
}

async function openSupportPanel(): Promise<HTMLElement> {
  await userEvent.click(await screen.findByTestId("nav-support-toggle"));
  return screen.getByTestId("adapter-support-list");
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("supported_visible", () => {
  it("lists release candidates as Supported, with the server's text, when the server says so", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar();
    const nav = await openAllGroups();

    const link = await within(nav).findByRole("link", { name: "Release candidates" });
    expect(link).toHaveAttribute("href", "/releases#release-signoff");
    await waitFor(() => expect(chipOf(nav, "Release candidates")).toHaveTextContent("Supported"));
    expect(chipOf(nav, "Release candidates")).toHaveAttribute("data-status", "supported");
    // Exactly the server's sentence, after the status word, and the link's name stays its label.
    expect(descriptionOf(link)).toBe(`Supported. ${MESSAGES.releaseSupported}`);
    expect(link).not.toHaveAttribute("aria-disabled");
  });

  it("names the supported reference connector, which has no page, in the support panel with its evidence count", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar();
    await openAllGroups();
    const list = await openSupportPanel();

    const connector = within(list).getByTestId("adapter-support-document_request_connector");
    await waitFor(() => expect(connector).toHaveAttribute("data-status", "supported"));
    expect(connector).toHaveTextContent("OpenAPI document-request connector");
    expect(connector).toHaveTextContent("Supported");
    expect(connector).toHaveTextContent(MESSAGES.connectorSupported);
    expect(connector).toHaveTextContent("Backed by 2 scenario tests.");
    // A supported adapter does not promote its neighbours.
    expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveAttribute(
      "data-status",
      "preview",
    );
    expect(within(list).getByTestId("adapter-support-openapi_integrations")).toHaveAttribute(
      "data-status",
      "preview",
    );
  });

  it("says 'test' for one cited scenario", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      document_request_connector: entry(
        "document_request_connector",
        "supported",
        MESSAGES.connectorSupported,
        { evidence: ["tests/egi_scenarios/x.py::test_only"] },
      ),
    });
    renderSidebar();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-document_request_connector")).toHaveTextContent(
        "Backed by 1 scenario test.",
      ),
    );
  });
});

describe("preview_label_retained", () => {
  it("keeps MCP servers and OpenAPI integrations Preview, with the server's reason, on a reference deployment", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar();
    const nav = await openAllGroups();

    await waitFor(() => expect(chipOf(nav, "MCP Servers")).toHaveTextContent("Preview"));
    expect(chipOf(nav, "OpenAPI Integrations")).toHaveTextContent("Preview");
    const mcp = within(nav).getByRole("link", { name: "MCP Servers" });
    expect(mcp).toHaveAttribute("href", "/mcp-servers");
    expect(descriptionOf(mcp)).toBe(`Preview. ${MESSAGES.mcpPreview}`);
    expect(descriptionOf(within(nav).getByRole("link", { name: "OpenAPI Integrations" }))).toBe(
      `Preview. ${MESSAGES.openapiPreview}`,
    );
  });

  it("does not promote anything because a flag is on: the flag lists the destination, only the server labels it", async () => {
    serve(ALL_ON, {
      mcp_servers: entry("mcp_servers", "preview", MESSAGES.mcpPreview),
      openapi_integrations: entry("openapi_integrations", "preview", MESSAGES.openapiPreview),
      release_evaluation: entry("release_evaluation", "preview", "preview text", {
        reason_code: "simulated_provider",
        disabled_by: "CALIBER_EVAL_PROVIDER",
      }),
    });
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(chipOf(nav, "Release candidates")).toBeInTheDocument());
    for (const label of ["Release candidates", "MCP Servers", "OpenAPI Integrations"]) {
      expect(chipOf(nav, label), label).toHaveAttribute("data-status", "preview");
    }
    expect(screen.queryByText("Supported")).not.toBeInTheDocument();
  });

  it("reads an older server's missing adapters, as Preview, never Supported, and says the server did not report", async () => {
    serve(ALL_ON); // flags but no ``adapters``
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(chipOf(nav, "MCP Servers")).toHaveAttribute("data-status", "preview"));
    expect(descriptionOf(within(nav).getByRole("link", { name: "MCP Servers" }))).toBe(
      "Preview. The server did not report why. Nothing is shown as supported unless the server says so.",
    );
    const list = await openSupportPanel();
    expect(within(list).getByTestId("adapter-support-document_request_connector")).toHaveAttribute(
      "data-status",
      "preview",
    );
    expect(within(list).queryByText("Supported")).not.toBeInTheDocument();
  });

  it.each([
    ["an unknown status (a made-up tier)", { status: "ga" }],
    ["a supported entry that cites no evidence", { evidence: [] }],
    ["a supported entry that carries a reason code", { reason_code: "no_support_evidence" }],
    ["a supported entry with no message", { message: "" }],
    ["a supported entry whose message is not text", { message: 7 }],
    ["a supported entry whose evidence is not a list", { evidence: "tests/x.py::t" }],
  ])("never reads %s as Supported", async (_name, patch) => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      release_evaluation: {
        ...entry("release_evaluation", "supported", MESSAGES.releaseSupported),
        ...patch,
      } as unknown as AdapterCapability,
    });
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(chipOf(nav, "Release candidates")).toBeInTheDocument());
    expect(chipOf(nav, "Release candidates")).toHaveAttribute("data-status", "preview");
    expect(screen.queryByText("Supported")).not.toBeInTheDocument();
  });

  it("falls back to the plain adapter name and the not-reported text for a nameless or null entry", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: null as unknown as AdapterCapability,
      openapi_integrations: {
        ...entry("openapi_integrations", "preview", MESSAGES.openapiPreview),
        label: "",
        message: "",
      },
    });
    renderSidebar();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveAttribute(
        "data-status",
        "preview",
      ),
    );
    const mcp = within(list).getByTestId("adapter-support-mcp_servers");
    expect(mcp).toHaveTextContent("MCP servers");
    expect(mcp).toHaveTextContent("The server did not report why.");
    const openapi = within(list).getByTestId("adapter-support-openapi_integrations");
    expect(openapi).toHaveTextContent("OpenAPI integrations");
    expect(openapi).toHaveTextContent("The server did not report why.");
  });
});

describe("unavailable_disabled", () => {
  it("shows an unavailable adapter as a disabled item with the server's reason, and does not navigate", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "unavailable", MESSAGES.mcpUnavailable, {
        reason_code: "mcp_not_in_reference_profile",
      }),
    });
    renderSidebar("/prompts");
    const nav = await openAllGroups();

    const disabled = await within(nav).findByTestId("nav-disabled-mcp_servers");
    expect(disabled).toHaveAttribute("role", "link");
    expect(disabled).toHaveAttribute("aria-disabled", "true");
    expect(disabled).not.toHaveAttribute("href");
    expect(disabled).toHaveAttribute("tabindex", "0");
    expect(disabled).toHaveAccessibleName("MCP Servers");
    expect(descriptionOf(disabled)).toBe(`Unavailable. ${MESSAGES.mcpUnavailable}`);
    expect(within(disabled).getByTestId("adapter-support-chip")).toHaveTextContent("Unavailable");

    // Neither a click nor Enter follows it: UI visibility grants no authority.
    await userEvent.click(disabled);
    disabled.focus();
    await userEvent.keyboard("{Enter}");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/prompts$/);
    // Its neighbour, which is preview and not unavailable, still navigates.
    await userEvent.click(within(nav).getByRole("link", { name: "OpenAPI Integrations" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/openapi-integrations$/);
  });

  it("disables release candidates while the evaluator is off, with the recovery step", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      release_evaluation: entry("release_evaluation", "unavailable", MESSAGES.evaluatorDisabled, {
        reason_code: "evaluator_disabled",
        disabled_by: "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED",
      }),
    });
    renderSidebar();
    const nav = await openAllGroups();
    const disabled = await within(nav).findByTestId("nav-disabled-release_evaluation");
    expect(descriptionOf(disabled)).toContain("CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED=true");
    expect(disabled).toHaveAccessibleName("Release candidates");
    expect(disabled).toHaveAttribute("aria-disabled", "true");
    expect(disabled).not.toHaveAttribute("href");
  });

  it("lists no destination for an area whose flag is off, and still gives the exact refusal in the support panel", async () => {
    serve(
      { aria: true },
      {
        document_request_connector: entry(
          "document_request_connector",
          "preview",
          MESSAGES.connectorPreview,
          { reason_code: "not_reference_profile", disabled_by: "CALIBER_PROFILE" },
        ),
        mcp_servers: entry("mcp_servers", "unavailable", MESSAGES.integrationsOff, {
          reason_code: "feature_disabled",
          disabled_by: "CALIBER_FEATURE_INTEGRATIONS",
        }),
      },
    );
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(within(nav).getByText("Plans")).toBeInTheDocument());
    for (const label of ["MCP Servers", "OpenAPI Integrations", "Release candidates", "Cookbooks"]) {
      expect(within(nav).queryByText(label)).not.toBeInTheDocument();
    }
    expect(screen.queryAllByTestId("adapter-support-chip")).toHaveLength(0);

    const list = await openSupportPanel();
    const mcp = within(list).getByTestId("adapter-support-mcp_servers");
    await waitFor(() => expect(mcp).toHaveAttribute("data-status", "unavailable"));
    expect(mcp).toHaveTextContent(MESSAGES.integrationsOff);
    expect(within(list).getByTestId("adapter-support-document_request_connector")).toHaveTextContent(
      MESSAGES.connectorPreview,
    );
  });
});

describe("alpha_navigation_regression", () => {
  const ALPHA_LABELS = [
    "Dashboard",
    "Prompts",
    "Test Sets",
    "Evaluations",
    "Releases",
    "Skills",
    "Tools",
    "Workflows",
    "Knowledge Bases",
    "Object Store",
    "Judges",
    "Agents",
    "Observability",
    "Review Queues",
    "Audit Log",
    "Plans",
    "LLM Gateway",
    "Administration",
    "Settings",
    "Docs",
  ];

  it("keeps the alpha navigation, and puts no label on it, with every flag off by default", async () => {
    serve({ aria: true });
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(within(nav).getByText("Plans")).toBeInTheDocument());
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.querySelector("span.flex-1")?.textContent?.trim()),
    ).toEqual(ALPHA_LABELS);
    expect(screen.queryAllByTestId("adapter-support-chip")).toHaveLength(0);
    // What does change on a default install: one new control under the navigation, closed. It is
    // not a destination (a button, not a link), so the link list above is exactly the alpha's.
    const toggle = screen.getByTestId("nav-support-toggle");
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle).toHaveAccessibleName("Support status");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(nav).getAllByRole("link")).toHaveLength(ALPHA_LABELS.length);
    // Alpha destinations carry no description either.
    for (const link of within(nav).getAllByRole("link")) {
      expect(link).not.toHaveAttribute("aria-describedby");
    }
  });

  it("leaves Releases a plain current-page link, and Release candidates current only at its own place", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar("/releases");
    const nav = await openAllGroups();
    const releases = await within(nav).findByRole("link", { name: "Releases" });
    expect(releases).toHaveAttribute("aria-current", "page");
    expect(releases).toHaveClass("nav-item-active");
    const candidates = within(nav).getByRole("link", { name: "Release candidates" });
    expect(candidates).not.toHaveAttribute("aria-current");
    expect(candidates).not.toHaveClass("nav-item-active");
    // Following it moves to its place on the page, and it becomes the current place.
    await userEvent.click(candidates);
    expect(screen.getByTestId("location")).toHaveTextContent("/releases#release-signoff");
    expect(candidates).toHaveAttribute("aria-current", "location");
    expect(candidates).toHaveClass("nav-item-active");
  });

  it("shows nothing, and no chip, while capabilities load", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, async () => {
        await delay(80);
        return HttpResponse.json(capabilitiesBody(ALL_ON, referenceAdapters()));
      }),
      http.get(`${API_BASE}/aria/plans`, () => HttpResponse.json({ data: [] })),
    );
    renderSidebar();
    const nav = await openAllGroups();
    expect(within(nav).queryByText("Release candidates")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("adapter-support-chip")).toHaveLength(0);
    const list = await openSupportPanel();
    for (const id of [
      "document_request_connector",
      "release_evaluation",
      "mcp_servers",
      "openapi_integrations",
    ]) {
      const row = within(list).getByTestId(`adapter-support-${id}`);
      expect(row).toHaveAttribute("data-status", "loading");
      expect(row).toHaveTextContent("Checking what this server supports…");
      expect(within(row).queryByTestId("adapter-support-chip")).not.toBeInTheDocument();
    }
    // ...and the answer arrives.
    expect(await within(nav).findByText("Release candidates")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-release_evaluation")).toHaveAttribute(
        "data-status",
        "supported",
      ),
    );
  });

  it("shows nothing as supported when capabilities cannot be loaded", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({ detail: "boom", status_code: 500 }, { status: 500 }),
      ),
    );
    renderSidebar();
    const nav = await openAllGroups();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-release_evaluation")).toHaveAttribute(
        "data-status",
        "preview",
      ),
    );
    expect(within(nav).queryByText("Release candidates")).not.toBeInTheDocument();
    expect(within(nav).queryByText("MCP Servers")).not.toBeInTheDocument();
    expect(within(list).queryByText("Supported")).not.toBeInTheDocument();
    expect(within(list).getAllByText("The server did not report why.", { exact: false })).toHaveLength(4);
  });
});

describe("support status disclosure", () => {
  it("is a closed, keyboard-operable disclosure that controls its list", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar();
    const toggle = await screen.findByTestId("nav-support-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "nav-support-panel");
    expect(document.getElementById("nav-support-panel")).not.toBeNull();
    expect(screen.queryByTestId("adapter-support-list")).not.toBeInTheDocument();

    toggle.focus();
    await userEvent.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = screen.getByRole("list", { name: "Support status by adapter" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);

    await userEvent.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("adapter-support-list")).not.toBeInTheDocument();
  });

  it("states each status in words as well as colour", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "unavailable", MESSAGES.mcpUnavailable),
    });
    renderSidebar();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveAttribute(
        "data-status",
        "unavailable",
      ),
    );
    const words = within(list)
      .getAllByTestId("adapter-support-chip")
      .map((chip) => chip.textContent);
    expect(words).toEqual(["Supported", "Supported", "Unavailable", "Preview"]);
    // The icon is decoration: the word carries the meaning.
    for (const chip of within(list).getAllByTestId("adapter-support-chip")) {
      expect(chip.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("hides the chips from the accessibility tree so a link keeps the destination's name", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(chipOf(nav, "MCP Servers")).toBeInTheDocument());
    const chip = chipOf(nav, "MCP Servers") as HTMLElement;
    expect(chip.parentElement).toHaveAttribute("aria-hidden", "true");
    expect(within(nav).getByRole("link", { name: "MCP Servers" })).toBeInTheDocument();
  });

  it("keeps the labels in the icon rail as tooltips and hides the panel there", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar("/", true);
    const nav = await screen.findByLabelText("CALIBER navigation");
    await waitFor(() =>
      expect(within(nav).getByRole("link", { name: "MCP Servers" })).toHaveAttribute(
        "title",
        "MCP Servers (Preview)",
      ),
    );
    expect(within(nav).getByRole("link", { name: "Release candidates" })).toHaveAttribute(
      "title",
      "Release candidates (Supported)",
    );
    expect(chipOf(nav, "MCP Servers")?.parentElement).toHaveClass("md:hidden");
    expect(screen.getByTestId("nav-support-toggle").closest("div.mt-3")).toHaveClass("md:hidden");
    // The popover has no room in the rail either.
    expect(within(nav).getByTestId("nav-support-text-mcp_servers")).toHaveClass("md:hidden");
  });
});

describe("what the server cannot make supported, and what it cannot make the UI render", () => {
  it.each(["mcp_servers", "openapi_integrations"] as const)(
    "caps %s at Preview even when the server reports a complete supported entry",
    async (id) => {
      serve(ALL_ON, {
        ...referenceAdapters(),
        [id]: entry(id, "supported", `${LABEL[id]} is supported: a server regression.`),
      });
      renderSidebar();
      const nav = await openAllGroups();
      const label = id === "mcp_servers" ? "MCP Servers" : "OpenAPI Integrations";
      await waitFor(() => expect(chipOf(nav, label)).toBeInTheDocument());
      expect(chipOf(nav, label)).toHaveAttribute("data-status", "preview");
      expect(chipOf(nav, label)).toHaveTextContent("Preview");
      const link = within(nav).getByRole("link", { name: label });
      expect(descriptionOf(link)).toBe(
        "Preview. This app shows only the reference connector and release evaluation as supported, so this adapter is shown as preview.",
      );
      const list = await openSupportPanel();
      const row = within(list).getByTestId(`adapter-support-${id}`);
      expect(row).toHaveAttribute("data-status", "preview");
      expect(row).not.toHaveTextContent("Backed by");
      expect(row).not.toHaveTextContent("a server regression");
    },
  );

  it("still lets the two reference adapters read Supported", async () => {
    serve(ALL_ON, referenceAdapters());
    renderSidebar();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-release_evaluation")).toHaveAttribute(
        "data-status",
        "supported",
      ),
    );
    expect(within(list).getByTestId("adapter-support-document_request_connector")).toHaveAttribute(
      "data-status",
      "supported",
    );
  });

  it("never shows 'Backed by' for a preview entry that lists evidence", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "preview", MESSAGES.mcpPreview, {
        evidence: ["tests/x.py::test_a", "tests/x.py::test_b"],
      }),
      release_evaluation: entry("release_evaluation", "unavailable", MESSAGES.evaluatorDisabled, {
        evidence: ["tests/x.py::test_c"],
      }),
    });
    renderSidebar();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveAttribute(
        "data-status",
        "preview",
      ),
    );
    expect(within(list).getByTestId("adapter-support-mcp_servers")).not.toHaveTextContent("Backed by");
    expect(within(list).getByTestId("adapter-support-release_evaluation")).not.toHaveTextContent(
      "Backed by",
    );
    // Only the supported connector cites its evidence.
    expect(within(list).getByTestId("adapter-support-document_request_connector")).toHaveTextContent(
      "Backed by 2 scenario tests.",
    );
  });

  it("keeps an unavailable entry with no message Unavailable, and says the server did not report why", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "unavailable", "", { reason_code: "mcp_not_in_reference_profile" }),
    });
    renderSidebar();
    const nav = await openAllGroups();
    const disabled = await within(nav).findByTestId("nav-disabled-mcp_servers");
    expect(within(disabled).getByTestId("adapter-support-chip")).toHaveAttribute("data-status", "unavailable");
    expect(descriptionOf(disabled)).toBe(
      "Unavailable. The server did not report why. Nothing is shown as supported unless the server says so.",
    );
  });

  it("renders markup, script and javascript: text in a label or message as inert text", async () => {
    const hostile = {
      label: '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script>',
      message: '<script>window.__pwned=1</script><a href="javascript:window.__pwned=1">click</a>',
    };
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "preview", hostile.message, { label: hostile.label }),
      release_evaluation: entry("release_evaluation", "unavailable", hostile.message, {
        label: hostile.label,
      }),
    });
    renderSidebar();
    const nav = await openAllGroups();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveTextContent(hostile.message),
    );
    expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveTextContent(hostile.label);
    // Text, not elements: nothing was parsed out of it.
    for (const root of [nav, list]) {
      expect(root.querySelector("script")).toBeNull();
      expect(root.querySelector("img")).toBeNull();
      expect(root.querySelector('a[href^="javascript:"]')).toBeNull();
    }
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(descriptionOf(within(nav).getByTestId("nav-disabled-release_evaluation"))).toBe(
      `Unavailable. ${hostile.message}`,
    );
  });

  it("renders a 5000-character message without breaking the item, the chip or the panel", async () => {
    const long = `${"reason ".repeat(715)}end`.slice(0, 5000);
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "preview", long),
    });
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(chipOf(nav, "MCP Servers")).toHaveTextContent("Preview"));
    expect(descriptionOf(within(nav).getByRole("link", { name: "MCP Servers" }))).toBe(`Preview. ${long}`);
    const list = await openSupportPanel();
    expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveTextContent(long);
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
  });

  it("gives each status its own chip style, so a swapped style is caught", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "unavailable", MESSAGES.mcpUnavailable),
    });
    renderSidebar();
    const list = await openSupportPanel();
    await waitFor(() =>
      expect(within(list).getByTestId("adapter-support-mcp_servers")).toHaveAttribute(
        "data-status",
        "unavailable",
      ),
    );
    const classOf = (id: string): string =>
      within(within(list).getByTestId(`adapter-support-${id}`)).getByTestId("adapter-support-chip")
        .className;
    expect(classOf("release_evaluation")).toContain("adapter-chip-supported");
    expect(classOf("release_evaluation")).not.toMatch(/adapter-chip-(preview|unavailable)/);
    expect(classOf("openapi_integrations")).toContain("adapter-chip-preview");
    expect(classOf("openapi_integrations")).not.toMatch(/adapter-chip-(supported|unavailable)/);
    expect(classOf("mcp_servers")).toContain("adapter-chip-unavailable");
    expect(classOf("mcp_servers")).not.toMatch(/adapter-chip-(supported|preview)/);
  });
});

describe("the reason is on the item, not only in the closed panel", () => {
  it("shows the server's text as a hover and focus popover on each labelled item, and keeps a disabled label at full strength", async () => {
    serve(ALL_ON, {
      ...referenceAdapters(),
      mcp_servers: entry("mcp_servers", "unavailable", MESSAGES.mcpUnavailable),
    });
    renderSidebar();
    const nav = await openAllGroups();
    const disabled = await within(nav).findByTestId("nav-disabled-mcp_servers");

    const popover = within(nav).getByTestId("nav-support-text-mcp_servers");
    expect(popover).toHaveTextContent(`Unavailable. ${MESSAGES.mcpUnavailable}`);
    // Revealed by hover and by keyboard focus on the row (a title is neither keyboard-reachable nor
    // reliably touch-reachable), hidden otherwise, and the item's description points at it.
    expect(popover.className).toContain("group-hover:block");
    expect(popover.className).toContain("group-focus-within:block");
    expect(popover.className).toMatch(/(^|\s)hidden(\s|$)/);
    expect(disabled).toHaveAttribute("aria-describedby", popover.id);
    expect(popover.parentElement).toHaveClass("group");
    // The disabled label is not dimmed (the status chip and cursor say it is disabled).
    expect(disabled.className).not.toMatch(/opacity/);
    expect(within(nav).getByTestId("nav-support-text-release_evaluation")).toHaveTextContent(
      `Supported. ${MESSAGES.releaseSupported}`,
    );
  });

  it("puts no popover on items that carry no support label", async () => {
    serve({ aria: true });
    renderSidebar();
    const nav = await openAllGroups();
    await waitFor(() => expect(within(nav).getByText("Plans")).toBeInTheDocument());
    expect(nav.querySelector('[data-testid^="nav-support-text-"]')).toBeNull();
  });
});
