import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import * as environment from "@/lib/environment";
import { Prompts } from "@/pages/Prompts";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

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

  /**
   * This is the configuration in which the old behaviour was Critical rather
   * than merely confusing. With one alias, "Save as New Version" promoted
   * straight to `@prod`: a control saying "save" changed what production
   * served, on the shipped default, with no confirmation step.
   */
  it("does not promote to prod when saving an edited prompt", async () => {
    let aliasCalls = 0;
    server.use(
      http.get(`${API_BASE}/prompts/support-agent`, () =>
        HttpResponse.json({
          data: {
            name: "support-agent",
            version: 3,
            alias: "prod",
            template: "You are support-agent v3",
            template_length: 24,
            artifact_ref: "prompts:/support-agent@prod",
          },
        }),
      ),
      http.post(`${API_BASE}/prompts/support-agent/versions`, () =>
        HttpResponse.json(
          {
            data: {
              name: "support-agent",
              version: 4,
              uri: "prompts:/support-agent/4",
              template_preview: "You are support-agent v4",
              template_length: 24,
            },
          },
          { status: 201 },
        ),
      ),
      http.post(`${API_BASE}/prompts/support-agent/aliases/:alias`, () => {
        aliasCalls += 1;
        return HttpResponse.json({
          data: { name: "support-agent", alias: "prod", version: 4 },
        });
      }),
    );

    const user = userEvent.setup();
    renderPrompts();
    await screen.findByRole("heading", { name: "Prompts" });

    await user.click((await screen.findAllByRole("button", { name: "Edit" }))[0]!);
    const template = (await screen.findByPlaceholderText(
      "Prompt template",
    )) as HTMLTextAreaElement;
    await user.clear(template);
    await user.type(template, "You are support-agent v4");

    await user.click(screen.getByTestId("prompt-edit-save"));

    // Saved, and production is untouched.
    expect(await screen.findByText(/Saved v4/)).toBeInTheDocument();
    expect(aliasCalls).toBe(0);

    // Going live is a second, explicitly-named act -- and with one alias the
    // destination it names is prod.
    const promote = screen.getByTestId("prompt-edit-promote");
    expect(promote).toHaveTextContent("Promote v4 to @prod");
    await user.click(promote);
    await waitFor(() => expect(aliasCalls).toBe(1));
  });
});
