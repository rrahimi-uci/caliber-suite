/**
 * FeatureGate: the route guard for the alpha's feature-flagged areas.
 *
 * A disabled area must say so (not a blank page, not a redirect), name the variable that turns
 * it on, and offer a way out. A default install must never render the guarded page, including
 * while capabilities are still loading or when they cannot be loaded at all.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { FeatureDisabledNotice, FeatureGate } from "@/components/FeatureGate";
import { capabilitiesBody } from "@/test/handlers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function renderAt(path: string, element: JSX.Element): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[path]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <CurrentPath />
        <Routes>
          <Route path="/" element={<div>Dashboard page</div>} />
          <Route path="/cookbooks" element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function gatedCookbooks(): JSX.Element {
  return (
    <FeatureGate feature="cookbooks">
      <div>Cookbook gallery</div>
    </FeatureGate>
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("FeatureGate", () => {
  it("renders the page when the server reports the area enabled", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ cookbooks: true })),
      ),
    );
    renderAt("/cookbooks", gatedCookbooks());
    expect(await screen.findByText("Cookbook gallery")).toBeInTheDocument();
    expect(
      screen.queryByTestId("feature-disabled-panel"),
    ).not.toBeInTheDocument();
  });

  it("explains a disabled area: heading, label, variable, and a way back", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody({ aria: true })),
      ),
    );
    renderAt("/cookbooks", gatedCookbooks());

    const panel = await screen.findByTestId("feature-disabled-panel");
    expect(
      screen.getByRole("heading", {
        name: "This area is disabled in this deployment",
      }),
    ).toBeInTheDocument();
    expect(panel).toHaveTextContent("Cookbooks");
    expect(panel).toHaveTextContent("CALIBER_FEATURE_COOKBOOKS=true");
    expect(panel).toHaveTextContent("Preview");
    expect(screen.queryByText("Cookbook gallery")).not.toBeInTheDocument();

    // Not a redirect: the URL stays where the user went.
    expect(screen.getByTestId("current-path")).toHaveTextContent("/cookbooks");
    await userEvent.click(
      screen.getByRole("link", { name: "Back to the dashboard" }),
    );
    expect(await screen.findByText("Dashboard page")).toBeInTheDocument();
    expect(screen.getByTestId("current-path")).toHaveTextContent("/");
  });

  it("labels a record-only area as record-only", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody()),
      ),
    );
    renderAt(
      "/cookbooks",
      <FeatureGate feature="release_candidates">
        <div>Candidates</div>
      </FeatureGate>,
    );
    const panel = await screen.findByTestId("feature-disabled-panel");
    expect(panel).toHaveTextContent(
      "Release-candidate signoff and workspace releases",
    );
    expect(panel).toHaveTextContent("CALIBER_FEATURE_RELEASE_CANDIDATES=true");
    expect(panel).toHaveTextContent("Record-only");
  });

  it("shows neither the page nor the disabled panel while capabilities load", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, async () => {
        await delay(60);
        return HttpResponse.json(capabilitiesBody({ cookbooks: true }));
      }),
    );
    renderAt("/cookbooks", gatedCookbooks());
    expect(screen.getByTestId("feature-gate-loading")).toBeInTheDocument();
    expect(screen.queryByText("Cookbook gallery")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("feature-disabled-panel"),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("Cookbook gallery")).toBeInTheDocument();
  });

  it("treats a failed capabilities request as disabled, and still names the variable", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          { detail: "boom", status_code: 500 },
          { status: 500 },
        ),
      ),
    );
    renderAt("/cookbooks", gatedCookbooks());
    const panel = await screen.findByTestId("feature-disabled-panel");
    expect(panel).toHaveTextContent("CALIBER_FEATURE_COOKBOOKS=true");
    expect(screen.queryByText("Cookbook gallery")).not.toBeInTheDocument();
  });

  it("treats a server that reports no features as disabled", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({
          data: { workflow_runs: {}, sync_workflow_version_run: true },
        }),
      ),
    );
    renderAt("/cookbooks", gatedCookbooks());
    expect(
      await screen.findByTestId("feature-disabled-panel"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Cookbook gallery")).not.toBeInTheDocument();
  });
});

describe("FeatureDisabledNotice", () => {
  function renderNotice(): void {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FeatureDisabledNotice feature="aria" what="Generating test cases">
            <a href="/eval-datasets">Test Sets</a>
          </FeatureDisabledNotice>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("says what is unavailable, why, and what still works", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(capabilitiesBody()),
      ),
    );
    renderNotice();
    const note = await screen.findByTestId("feature-disabled-notice");
    expect(note).toHaveTextContent(
      "Generating test cases needs Aria assistant and plans, which is disabled in this deployment.",
    );
    expect(note).toHaveTextContent("CALIBER_FEATURE_ARIA=true");
    expect(screen.getByRole("link", { name: "Test Sets" })).toBeInTheDocument();
  });

  it("says nothing until the server has answered", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, async () => {
        await delay(50);
        return HttpResponse.json(capabilitiesBody());
      }),
    );
    renderNotice();
    expect(
      screen.queryByTestId("feature-disabled-notice"),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("feature-disabled-notice")).toBeInTheDocument(),
    );
  });
});
