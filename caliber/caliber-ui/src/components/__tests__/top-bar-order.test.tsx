import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AssistantPanelProvider } from "@/components/assistant/AssistantPanelContext";
import { TopBar } from "@/components/TopBar";

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

describe("TopBar action order", () => {
  it("places context and primary action before utilities, with the account last", () => {
    render(
      <AssistantPanelProvider>
        <TopBar health="ok" currentUser="@admin" onLogout={vi.fn()} />
      </AssistantPanelProvider>,
    );

    const workspace = screen.getByRole("button", { name: "Active workspace" });
    const aria = screen.getByRole("button", { name: "Ask Aria" });
    const health = screen.getByLabelText("API and database reachable");
    const theme = screen.getByRole("button", { name: /Switch to dark mode/i });
    const account = screen.getByText("@admin");

    expectBefore(workspace, aria);
    expectBefore(aria, health);
    expectBefore(health, theme);
    expectBefore(theme, account);
  });
});
