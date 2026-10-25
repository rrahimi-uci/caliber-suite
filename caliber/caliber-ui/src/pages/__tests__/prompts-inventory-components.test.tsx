import { describe, expect, it, vi } from "vitest";

import type { PromptInfo } from "@/api/types";
import {
  NeedsPromptCard,
  NeedsPromptRow,
  PromptCard,
  PromptRow,
} from "@/pages/Prompts";
import { render, screen, userEvent } from "@/test/utils";

const prompt = (overrides: Partial<PromptInfo> = {}): PromptInfo => ({
  agent_id: "agent-1",
  agent_name: "Support Agent",
  agent_enabled: true,
  prompt_name: "support",
  version: 3,
  alias: "prod",
  available_aliases: ["prod", "staging", "dev"],
  template_preview: "Answer support questions.",
  template_length: 28,
  approval_id: "approval-123456789",
  artifact_ref: null,
  has_prompt: true,
  needs_prompt: false,
  source: "both",
  ...overrides,
});

function callbacks() {
  return {
    onOpen: vi.fn(),
    onEdit: vi.fn(),
    onVersions: vi.fn(),
    onCreate: vi.fn(),
    onDelete: vi.fn(),
  };
}

describe("prompt inventory components", () => {
  it("renders deployed card and list variants with every optional affordance", async () => {
    const user = userEvent.setup();
    const handlers = callbacks();
    const value = prompt();
    render(
      <>
        <PromptCard prompt={value} isAdmin deleting={false} {...handlers} />
        <PromptRow prompt={value} isAdmin deleting={false} {...handlers} />
      </>,
    );

    expect(screen.getByTestId("prompt-card-agent-1")).toHaveTextContent("Deployed");
    expect(screen.getByTestId("prompt-row-agent-1")).toHaveTextContent("Approval linked");
    expect(screen.getAllByText("@staging")).toHaveLength(1);
    expect(screen.getAllByText("@dev")).toHaveLength(1);
    await user.click(screen.getAllByRole("button", { name: "Versions" })[0]!);
    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]!);
    await user.click(screen.getAllByRole("button", { name: "Delete prompt Support Agent" })[0]!);
    expect(handlers.onVersions).toHaveBeenCalled();
    expect(handlers.onEdit).toHaveBeenCalled();
    expect(handlers.onDelete).toHaveBeenCalled();
  });

  it.each([
    ["mlflow", true, "No preview available."],
    ["caliber", false, "No deployed prompt for this agent yet."],
    ["unknown", false, "No deployed prompt for this agent yet."],
  ] as const)("covers card/list fallback states for %s source", async (source, hasPrompt, fallback) => {
    const user = userEvent.setup();
    const handlers = callbacks();
    const value = prompt({
      agent_id: `agent-${source}`,
      source,
      has_prompt: hasPrompt,
      prompt_name: hasPrompt ? "support" : source === "caliber" ? "draft-support" : null,
      template_preview: "",
      version: hasPrompt ? 1 : null,
      alias: "",
      available_aliases: [],
      approval_id: null,
    });
    render(
      <>
        <PromptCard prompt={value} isAdmin={false} deleting={false} {...handlers} />
        <PromptRow prompt={value} isAdmin={false} deleting={false} {...handlers} />
      </>,
    );
    expect(screen.getAllByText(fallback).length).toBeGreaterThan(0);
    if (hasPrompt) {
      await user.click(screen.getAllByRole("button", { name: "Open" })[0]!);
      expect(handlers.onOpen).toHaveBeenCalled();
    } else if (value.prompt_name) {
      await user.click(screen.getAllByRole("button", { name: "Create" })[0]!);
      expect(handlers.onCreate).toHaveBeenCalled();
    } else {
      expect(screen.getAllByText("No deployed prompt").length).toBeGreaterThan(0);
    }
  });

  it("covers backlog card and list actions", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    const value = prompt({
      agent_id: "workflow-node",
      agent_name: "Workflow / Node",
      prompt_name: null,
      has_prompt: false,
      needs_prompt: true,
      template_preview: "",
      source: "mlflow",
    });
    render(
      <>
        <NeedsPromptCard prompt={value} onCreate={onCreate} />
        <NeedsPromptRow prompt={value} onCreate={onCreate} />
      </>,
    );
    expect(screen.getByTestId("needs-prompt-card-workflow-node")).toHaveTextContent("No deployed prompt");
    expect(screen.getByTestId("needs-prompt-row-workflow-node")).toHaveTextContent("Needs prompt");
    await user.click(screen.getAllByRole("button", { name: "Create prompt" })[0]!);
    await user.click(screen.getAllByRole("button", { name: "Create prompt" })[1]!);
    expect(onCreate).toHaveBeenCalledTimes(2);
  });
});
