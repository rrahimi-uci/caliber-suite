import { describe, expect, it } from "vitest";

import type { WorkflowRunAcceptance } from "@/api/workflowTypes";
import {
  WorkflowRunAcceptanceBadge,
  workflowRunAcceptanceLabel,
  workflowRunAcceptanceTitle,
} from "@/components/workflows/WorkflowRunAcceptanceBadge";
import { render, screen } from "@/test/utils";

function acceptance(overrides: Partial<WorkflowRunAcceptance> = {}): WorkflowRunAcceptance {
  return {
    status: "accepted",
    criteria_version: "harborlight-v1",
    error_code: null,
    decisions: [
      {
        decision_id: "acc-1",
        candidate_id: "prop-1",
        outcome: "accepted",
        observation_digest: "a".repeat(64),
        failed_criteria: [],
      },
    ],
    ...overrides,
  };
}

describe("workflow run acceptance", () => {
  it("shows nothing for a run with no acceptance information (a legacy run is never relabelled)", () => {
    expect(workflowRunAcceptanceLabel(null)).toBeNull();
    expect(workflowRunAcceptanceLabel(undefined)).toBeNull();
    expect(workflowRunAcceptanceTitle(null)).toBeNull();
    const { container } = render(<WorkflowRunAcceptanceBadge run={{ acceptance: null }} />);
    expect(container).toBeEmptyDOMElement();
    const absent = render(<WorkflowRunAcceptanceBadge run={{}} />);
    expect(absent.container).toBeEmptyDOMElement();
  });

  it("labels an accepted run and says what confirmed it", () => {
    render(<WorkflowRunAcceptanceBadge run={{ acceptance: acceptance() }} dataTestId="badge" />);
    const badge = screen.getByTestId("badge");
    expect(badge).toHaveTextContent("Acceptance: accepted");
    expect(badge).toHaveAttribute(
      "title",
      "1 effect confirmed by the downstream's own record (harborlight-v1).",
    );
    expect(badge.className).toContain("emerald");
  });

  it("names the criteria that failed on a rejected run, and never reads as success", () => {
    const rejected = acceptance({
      status: "rejected",
      decisions: [
        {
          decision_id: "acc-2",
          candidate_id: "prop-2",
          outcome: "rejected",
          observation_digest: "b".repeat(64),
          failed_criteria: ["attached_to_expected_version"],
        },
      ],
    });
    render(<WorkflowRunAcceptanceBadge run={{ acceptance: rejected }} dataTestId="badge" />);
    const badge = screen.getByTestId("badge");
    expect(badge).toHaveTextContent("Acceptance: rejected");
    expect(badge.getAttribute("title")).toBe(
      "The downstream's own record did not confirm the effect. Failed criteria: attached_to_expected_version (prop-2).",
    );
    expect(badge.className).toContain("red");
  });

  it("says a rejection with no failed criterion is an unreadable record, and an error is not an acceptance", () => {
    expect(workflowRunAcceptanceTitle(acceptance({ status: "rejected", decisions: [] }))).toBe(
      "The downstream's own record could not be read as evidence of the effect.",
    );
    const failed = acceptance({ status: "error", error_code: "decision_conflict", decisions: [] });
    expect(workflowRunAcceptanceLabel(failed)).toBe("Acceptance: could not be judged");
    expect(workflowRunAcceptanceTitle(failed)).toBe(
      "The run's effects could not be judged (decision_conflict), so the run is not accepted.",
    );
    expect(
      workflowRunAcceptanceTitle(acceptance({ status: "error", error_code: null, decisions: [] })),
    ).toContain("(unknown)");
  });

  it("pluralises the confirmed effects and tolerates an unversioned set", () => {
    const two = acceptance({
      criteria_version: null,
      decisions: [acceptance().decisions[0], acceptance().decisions[0]],
    });
    expect(workflowRunAcceptanceTitle(two)).toBe(
      "2 effects confirmed by the downstream's own record (unversioned criteria).",
    );
  });

  it("shows a status this build does not know as what it is, in a neutral tone", () => {
    const unknown = { ...acceptance(), status: "pending" } as unknown as WorkflowRunAcceptance;
    render(<WorkflowRunAcceptanceBadge run={{ acceptance: unknown }} dataTestId="badge" />);
    const badge = screen.getByTestId("badge");
    expect(badge).toHaveTextContent("Acceptance: pending");
    expect(badge.className).toContain("slate");
  });
});
