import { describe, expect, it, vi } from "vitest";

import {
  Disclosure,
  EmptyPanel,
  KnowledgeGuidancePanel,
  KbBuildStepper,
  SummaryRow,
} from "@/pages/KnowledgeBases";
import { render, screen, userEvent } from "@/test/utils";

describe("knowledge-base presentation states", () => {
  it("covers disclosure collapsed and expanded content", async () => {
    const user = userEvent.setup();
    render(
      <Disclosure summary="Advanced" hint="Optional settings" testId="advanced">
        <div>Advanced controls</div>
      </Disclosure>,
    );
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Advanced controls")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Advanced controls")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    expect(screen.queryByText("Advanced controls")).not.toBeInTheDocument();
  });

  it("renders every build step tone for active, failed, and completed runs", () => {
    const event = (event_type: string) => ({ event_type }) as never;
    const run = (status: string) => ({ status }) as never;
    const { rerender } = render(<KbBuildStepper run={run("running")} events={[]} />);
    expect(screen.getByTestId("kb-build-step-queued")).toHaveAttribute("data-state", "active");
    rerender(
      <KbBuildStepper
        run={run("failed")}
        events={[event("build_started"), event("source_failed")]}
      />,
    );
    expect(screen.getByTestId("kb-build-step-extract")).toHaveAttribute("data-state", "failed");
    rerender(<KbBuildStepper run={run("completed")} events={[]} />);
    expect(screen.getByTestId("kb-build-step-ready")).toHaveAttribute("data-state", "done");
    expect(screen.getAllByText("→")).toHaveLength(6);
  });

  it.each([
    ["graph", true, "AGE is not enabled"],
    ["playground", false, "AGE is unavailable for this deployment"],
  ] as const)("renders %s guidance for AGE enabled=%s", (focus, ageEnabled, unavailable) => {
    const onOpenBuild = vi.fn();
    render(
      <KnowledgeGuidancePanel
        title="Choose a build"
        body="Build a version first."
        focus={focus}
        ageEnabled={ageEnabled}
        ageUnavailableReason={unavailable}
        onOpenBuild={onOpenBuild}
      />,
    );
    expect(screen.getByTestId(`kb-onboarding-${focus}`)).toHaveTextContent(
      ageEnabled ? "Cypher-first retrieval is available" : unavailable,
    );
    expect(screen.getByRole("button", { name: "Open Build tab" })).toBeInTheDocument();
  });

  it("covers the compact summary and empty panel", () => {
    render(
      <>
        <SummaryRow label="Status" value="Ready" />
        <SummaryRow label="Version" value="KBV-1" mono />
        <EmptyPanel title="No selection" body="Choose a knowledge base." />
      </>,
    );
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("KBV-1")).toHaveClass("font-mono");
    expect(screen.getByText("No selection")).toBeInTheDocument();
  });
});
