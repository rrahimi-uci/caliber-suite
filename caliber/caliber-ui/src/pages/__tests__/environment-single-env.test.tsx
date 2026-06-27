import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import * as environment from "@/lib/environment";
import { Prompts } from "@/pages/Prompts";
import { server } from "@/test/server";

// This suite asserts the real, shipping single-environment defaults — there is
// deliberately NO vi.mock of "@/lib/environment" here. The dormant multi-stage
// (dev/staging/prod) UI is covered, with the flag mocked false, in
// prompts.test.tsx and workflow-studio.test.tsx.

function renderPrompts(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
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

// The Create Prompt builder is a 3-step wizard (Start → Compose → Save).
async function gotoSaveStep(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  const buildFromTemplate = screen.queryByRole("button", {
    name: /Build from template/i,
  });
  if (buildFromTemplate) {
    await user.click(buildFromTemplate);
  }
  const next = await screen.findByRole("button", { name: /Next: Compose/i });
  await waitFor(() => expect(next).toBeEnabled());
  await user.click(next);
  await user.click(
    await screen.findByRole("button", { name: /Next: Review/i }),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("single-environment defaults", () => {
  it("ships with one live alias and no stage ladder", () => {
    expect(environment.SINGLE_ENVIRONMENT).toBe(true);
    expect(environment.LIVE_ALIAS).toBe("prod");
    expect([...environment.DEPLOYMENT_ALIASES]).toEqual(["prod"]);
  });

  it("hides the deployment-alias selector in the prompt create wizard", async () => {
    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    // The promptless agent's backlog card opens the builder via "Create prompt".
    await user.click(
      await screen.findByRole("button", { name: "Create prompt" }),
    );
    expect(
      screen.getByRole("heading", { name: "Create Prompt" }),
    ).toBeInTheDocument();

    await gotoSaveStep(user);
    await screen.findByLabelText("Prompt name");

    // No stage selector and no staging-first hint copy in single-env mode.
    expect(screen.queryByText("Deployment alias")).toBeNull();
    expect(
      screen.queryByText(/safe default for testing and calibration/i),
    ).toBeNull();
  });
});
