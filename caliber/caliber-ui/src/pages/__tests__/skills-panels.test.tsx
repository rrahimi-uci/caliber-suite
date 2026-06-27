import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { caliberApi } from "@/api/caliberApi";
import type { Skill } from "@/api/types";
import { SkillPlaygroundPanel } from "@/pages/Skills";

const NOW = "2026-01-01T00:00:00Z";

function makeSkill(overrides: Partial<Skill> = {}): Skill {
  return {
    skill_id: "sk-1",
    name: "policy-answering",
    description: "Answers policy questions",
    summary: "Policy helper",
    content: "Hello {{customer_name}}, your policy is {{policy_id}}.",
    owner: "@team",
    category: "custom",
    tags: ["policy"],
    skill_metadata: {},
    allowed_tools: null,
    depends_on: [],
    status: "active",
    version: 1,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SkillPlaygroundPanel (Render Preview)", () => {
  it("shows loading and empty states", () => {
    const { rerender } = render(<SkillPlaygroundPanel skills={[]} loading />);
    expect(screen.getAllByText((_, el) => el?.className.includes("shimmer") ?? false).length).toBeGreaterThan(0);
    rerender(<SkillPlaygroundPanel skills={[]} loading={false} />);
    expect(screen.getByText("No skills available for playground rendering")).toBeInTheDocument();
  });

  it("renders a skill and handles variable parsing + render result", async () => {
    vi.spyOn(caliberApi, "testRenderSkill").mockResolvedValue({
      skill_id: "sk-1",
      skill_name: "policy-answering",
      rendered_content: "Hello Ada, your policy is refund-30.",
      original_content: "Hello {{customer_name}}, your policy is {{policy_id}}.",
      detected_variables: ["customer_name", "policy_id"],
      unresolved_variables: [],
      variables_applied: { customer_name: "Ada", policy_id: "refund-30" },
      summary: "Policy helper",
      duration_ms: 12,
      word_count: 7,
      char_count: 37,
    });

    const user = userEvent.setup();
    render(<SkillPlaygroundPanel skills={[makeSkill()]} loading={false} />);
    expect(await screen.findByText("Detected Variables")).toBeInTheDocument();
    expect(screen.getByText("customer_name")).toBeInTheDocument();
    expect(screen.getByText("policy_id")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("skill-playground-variables"), {
      target: { value: "[]" },
    });
    await user.click(screen.getByTestId("skill-playground-render"));
    expect(await screen.findByText("Variables must be a JSON object.")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("skill-playground-variables"), {
      target: { value: '{"customer_name":"Ada","policy_id":"refund-30"}' },
    });
    await user.click(screen.getByTestId("skill-playground-render"));
    expect(await screen.findByText("Rendered Output")).toBeInTheDocument();
    expect(screen.getByText(/Hello Ada/)).toBeInTheDocument();
  });
});
