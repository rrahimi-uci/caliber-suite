/**
 * View-toggle wiring on card-based list pages.
 *
 * Covers the shared {@link ViewToggle} + {@link useViewMode} behavior end to end
 * on two representative pages (Prompts inventory, Knowledge Bases library):
 * the default is the card grid; clicking "List" swaps to rows for the same
 * already-filtered items; the choice persists to localStorage and is read back
 * on a fresh mount.
 */

import { render as rtlRender } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render, screen, userEvent, waitFor } from "@/test/utils";

import { KnowledgeBases } from "@/pages/KnowledgeBases";
import { Prompts } from "@/pages/Prompts";
import { server } from "@/test/server";

// These pages fire some data requests lazily (e.g. per-KB runs/versions) that
// aren't needed by the landing list under test; bypass unmatched requests so
// the toggle assertions stay focused and noise-free.
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

// Prompts owns its own router (the shared `render` util also injects one, which
// would nest two Routers), so mount it with a bare RTL render + a single Router.
function renderPrompts(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  rtlRender(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/prompts"]}
      >
        <Routes>
          <Route path="/prompts" element={<Prompts />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Prompts view toggle", () => {
  it("defaults to the card grid", async () => {
    renderPrompts();

    // Cards render for both groups (mock: support-agent deployed, billing-agent needs prompt).
    expect(await screen.findByTestId("prompt-card-support-agent")).toBeInTheDocument();
    expect(screen.getByTestId("needs-prompt-card-billing-agent")).toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-grid")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("prompt-row-support-agent")).not.toBeInTheDocument();
  });

  it("switches to grouped rows when List is chosen", async () => {
    const user = userEvent.setup();
    renderPrompts();

    await screen.findByTestId("prompt-card-support-agent");
    await user.click(screen.getByTestId("view-toggle-list"));

    // Same items now render as rows, inside the same Deployed / Needs-prompt groups.
    expect(await screen.findByTestId("prompt-row-support-agent")).toBeInTheDocument();
    expect(screen.getByTestId("needs-prompt-row-billing-agent")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-group-deployed")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-group-needs-prompt")).toBeInTheDocument();
    // Cards are gone.
    expect(screen.queryByTestId("prompt-card-support-agent")).not.toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-list")).toHaveAttribute("aria-pressed", "true");
  });

  it("persists the list choice across a fresh mount", async () => {
    window.localStorage.setItem("caliber.viewmode.prompts", "list");
    renderPrompts();

    // Re-mounting reads the persisted choice → rows, not cards.
    expect(await screen.findByTestId("prompt-row-support-agent")).toBeInTheDocument();
    expect(screen.queryByTestId("prompt-card-support-agent")).not.toBeInTheDocument();
  });
});

describe("Knowledge Bases library view toggle", () => {
  it("defaults to the card grid", async () => {
    render(<KnowledgeBases />);

    expect(await screen.findByTestId("kb-card-KB-1")).toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-grid")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("kb-row-KB-1")).not.toBeInTheDocument();
  });

  it("switches to rows when List is chosen", async () => {
    const user = userEvent.setup();
    render(<KnowledgeBases />);

    await screen.findByTestId("kb-card-KB-1");
    await user.click(screen.getByTestId("view-toggle-list"));

    expect(await screen.findByTestId("kb-row-KB-1")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-card-KB-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-list")).toHaveAttribute("aria-pressed", "true");
  });

  it("persists the list choice across a fresh mount", async () => {
    window.localStorage.setItem("caliber.viewmode.knowledge-bases", "list");
    render(<KnowledgeBases />);

    await waitFor(() =>
      expect(screen.getByTestId("kb-row-KB-1")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("kb-card-KB-1")).not.toBeInTheDocument();
  });
});
