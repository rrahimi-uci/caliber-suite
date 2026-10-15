import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AssistantPanelProvider } from "@/components/assistant/AssistantPanelContext";
import { TopBar } from "@/components/TopBar";
import { server } from "@/test/server";

vi.mock("@/components/WorkspaceSelector", () => ({
  WorkspaceSelector: () => (
    <button type="button" aria-label="Active workspace">
      Workspace
    </button>
  ),
}));

function expectBefore(first: Element, second: Element): void {
  expect(
    Boolean(
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
    ),
  ).toBe(true);
}

// The Ask Aria button is behind the ``aria`` feature flag, so the bar reads ``/capabilities``
// (the default test handler reports every flag on).
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

describe("TopBar action order", () => {
  it("places context and primary action before utilities, with the account last", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AssistantPanelProvider>
          <TopBar health="ok" currentUser="@admin" onLogout={vi.fn()} />
        </AssistantPanelProvider>
      </QueryClientProvider>,
    );

    const workspace = screen.getByRole("button", { name: "Active workspace" });
    const aria = await screen.findByRole("button", { name: "Ask Aria" });
    const health = screen.getByLabelText("API and database reachable");
    const theme = screen.getByRole("button", { name: /Switch to dark mode/i });
    const account = screen.getByText("@admin");

    expectBefore(workspace, aria);
    expectBefore(aria, health);
    expectBefore(health, theme);
    expectBefore(theme, account);
  });
});
