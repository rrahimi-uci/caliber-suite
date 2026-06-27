import { describe, expect, it } from "vitest";

import type { AriaPlan } from "@/api/types";
import {
  isResumablePlanStatus,
  pickResumablePlan,
} from "@/components/aria/planView";

function plan(status: AriaPlan["status"], id = "P"): AriaPlan {
  return {
    plan_id: id,
    session_id: "S",
    project_id: null,
    goal: "g",
    status,
    autonomy: "approve_plan",
    owner: "@me",
    constraints: {},
    done_when: [],
    context_refs: [],
    created_at: "",
    updated_at: "",
    step_count: 0,
  };
}

describe("plan-view helpers", () => {
  it("treats completed/failed/cancelled as terminal, the rest as resumable", () => {
    expect(isResumablePlanStatus("completed")).toBe(false);
    expect(isResumablePlanStatus("failed")).toBe(false);
    expect(isResumablePlanStatus("cancelled")).toBe(false);
    expect(isResumablePlanStatus("paused")).toBe(true);
    expect(isResumablePlanStatus("draft")).toBe(true);
    expect(isResumablePlanStatus("running")).toBe(true);
    expect(isResumablePlanStatus("approved")).toBe(true);
  });

  it("picks the most recent still-open plan (input is newest-first)", () => {
    const picked = pickResumablePlan([
      plan("completed", "A"),
      plan("paused", "B"),
      plan("draft", "C"),
    ]);
    expect(picked?.plan_id).toBe("B");
  });

  it("returns null when every plan is terminal", () => {
    expect(
      pickResumablePlan([plan("completed", "A"), plan("cancelled", "B")]),
    ).toBeNull();
    expect(pickResumablePlan([])).toBeNull();
  });
});
