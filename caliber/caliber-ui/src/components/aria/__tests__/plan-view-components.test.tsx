import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AriaInteraction, AriaPlanStep } from "@/api/types";
import {
  PlanInteractionPrompt,
  PlanStatusBadge,
  PlanStepRow,
  StepStatusBadge,
} from "@/components/aria/planView";

const NOW = "2026-06-21T18:00:00Z";

function makeStep(overrides: Partial<AriaPlanStep> = {}): AriaPlanStep {
  return {
    step_id: "STP-1",
    plan_id: "P-1",
    seq: 0,
    capability_key: "send_email",
    title: "",
    inputs: {},
    depends_on: [],
    status: "pending",
    result: {},
    evidence: {},
    error: null,
    draft_id: null,
    job_id: null,
    approval_id: null,
    checkpoint_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeInteraction(overrides: Partial<AriaInteraction> = {}): AriaInteraction {
  return {
    interaction_id: "INT-1",
    plan_id: "P-1",
    step_id: "STP-1",
    kind: "permission",
    prompt: "May Aria proceed?",
    options: [],
    evidence: {},
    required_scope: null,
    status: "pending",
    response: {},
    responded_by: null,
    responded_at: null,
    created_at: NOW,
    ...overrides,
  };
}

describe("PlanStatusBadge", () => {
  it("renders the status text and title for every plan status", () => {
    (
      [
        "draft",
        "approved",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
      ] as const
    ).forEach((status) => {
      const { unmount } = render(<PlanStatusBadge status={status} />);
      expect(screen.getByTitle(status)).toHaveTextContent(status);
      unmount();
    });
  });
});

describe("StepStatusBadge", () => {
  it("renders the raw status label", () => {
    render(<StepStatusBadge status="done" />);
    expect(screen.getByTitle("done")).toHaveTextContent("done");
  });

  it("replaces underscores with spaces for multi-word statuses", () => {
    render(<StepStatusBadge status="waiting_input" />);
    expect(screen.getByTitle("waiting_input")).toHaveTextContent(
      "waiting input",
    );
  });
});

describe("PlanStepRow", () => {
  it("falls back to the capability key when the step has no title", () => {
    render(
      <ul>
        <PlanStepRow step={makeStep({ seq: 2, title: "", capability_key: "send_email" })} />
      </ul>,
    );
    // The capability key shows up both as the fallback title and in the
    // dedicated <code> chip, since the title span uses it when title is blank.
    expect(screen.getAllByText("send_email")).toHaveLength(2);
    // seq is zero-indexed; the badge shows seq + 1.
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("prefers an explicit title over the capability key", () => {
    render(
      <ul>
        <PlanStepRow step={makeStep({ title: "Send the welcome email" })} />
      </ul>,
    );
    expect(screen.getByText("Send the welcome email")).toBeInTheDocument();
  });

  it("shows depends-on count only when the step has dependencies", () => {
    const { rerender } = render(
      <ul>
        <PlanStepRow step={makeStep({ depends_on: [] })} />
      </ul>,
    );
    expect(screen.queryByText(/depends on/)).not.toBeInTheDocument();

    rerender(
      <ul>
        <PlanStepRow step={makeStep({ depends_on: ["STP-0", "STP-a"] })} />
      </ul>,
    );
    expect(screen.getByText("depends on 2 prior step(s)")).toBeInTheDocument();
  });

  it("lists required inputs only when input_schema.required is a string array", () => {
    const { rerender } = render(
      <ul>
        <PlanStepRow step={makeStep({ input_schema: undefined })} />
      </ul>,
    );
    expect(screen.queryByText(/^inputs:/)).not.toBeInTheDocument();

    rerender(
      <ul>
        <PlanStepRow
          step={makeStep({
            input_schema: { required: "not-an-array" },
          })}
        />
      </ul>,
    );
    expect(screen.queryByText(/^inputs:/)).not.toBeInTheDocument();

    rerender(
      <ul>
        <PlanStepRow
          step={makeStep({
            // A non-string entry must be filtered out alongside the valid ones.
            input_schema: { required: ["to", 7, "subject"] },
          })}
        />
      </ul>,
    );
    expect(screen.getByText("inputs: to, subject")).toBeInTheDocument();
  });

  it("renders the step error when present", () => {
    render(
      <ul>
        <PlanStepRow step={makeStep({ error: "SMTP timeout" })} />
      </ul>,
    );
    expect(screen.getByText("SMTP timeout")).toBeInTheDocument();
  });
});

describe("PlanInteractionPrompt — confirm kind", () => {
  it("shows gate evidence and Accept/Reject buttons when a metric is present", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "confirm",
          prompt: "Quality gate not met.",
          evidence: { metric: "faithfulness", min: 0.9, value: 0.72 },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    expect(screen.getByText("Aria needs you to confirm")).toBeInTheDocument();
    expect(screen.getByText(/Below quality gate/)).toBeInTheDocument();
    expect(screen.getByText(/faithfulness: 0.72/)).toBeInTheDocument();
    expect(screen.getByText(/needs ≥ 0.9/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Accept" }));
    expect(onAnswer).toHaveBeenCalledWith({ approved: true });

    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(onAnswer).toHaveBeenCalledWith({ approved: false });
  });

  it("hides the gate-evidence callout when no metric is present", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "confirm", evidence: {} })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Below quality gate/)).not.toBeInTheDocument();
  });

  it("disables both buttons while pending", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "confirm" })}
        pending
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });
});

describe("PlanInteractionPrompt — permission/approval kind", () => {
  it("shows the required-scope chip and separation-of-duties note, with Approve/Deny buttons", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "permission",
          required_scope: "caliber.admin",
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    expect(screen.getByText("Aria needs your approval")).toBeInTheDocument();
    expect(screen.getByText("requires caliber.admin")).toBeInTheDocument();
    expect(screen.getByText(/Separation of duties/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAnswer).toHaveBeenCalledWith({ approved: true });
    await user.click(screen.getByRole("button", { name: "Deny" }));
    expect(onAnswer).toHaveBeenCalledWith({ approved: false });
  });

  it("omits the required-scope UI when there is none", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "permission", required_scope: null })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Separation of duties/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^requires /)).not.toBeInTheDocument();
  });
});

describe("PlanInteractionPrompt — choice kind", () => {
  it("renders one button per option and answers with its value/label", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "choice",
          options: [
            { label: "Retry now", value: "retry" },
            { label: "Skip", value: "skip" },
          ],
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Retry now" }));
    expect(onAnswer).toHaveBeenCalledWith({ value: "retry", choice: "Retry now" });
  });

  it("falls back to Approve/Deny when the choice has no options", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "choice", options: [] })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });
});

describe("PlanInteractionPrompt — input kind", () => {
  const schema = {
    input_schema: {
      properties: {
        to: { type: "string", title: "Recipient" },
        cc: { type: "string" },
        priority: { type: "string", enum: ["low", "high"] },
        notify: { type: "boolean", title: "Notify on send" },
        count: { type: "integer" },
        score: { type: "number" },
        payload: { type: "object", description: "Extra JSON payload" },
      },
      required: ["to"],
    },
  };

  it("renders a control per editable input and excludes step-reference inputs", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            ...schema,
            current_inputs: { to: "a@b.com", cc: { $from_step: "STP-0" } },
          },
        })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByText("Aria needs information")).toBeInTheDocument();
    // `to` is editable (not a step reference).
    expect(screen.getByText("Recipient")).toBeInTheDocument();
    // `cc` resolves from a prior step, so it must not be rendered as an editable field.
    expect(screen.queryByText("cc")).not.toBeInTheDocument();
  });

  it("pre-fills string/number/boolean/JSON inputs from current_inputs by schema type", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            ...schema,
            current_inputs: {
              to: "existing@example.com",
              notify: true,
              count: 3,
              payload: { a: 1 },
            },
          },
        })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue("existing@example.com")).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByDisplayValue("3")).toBeInTheDocument();
    expect(screen.getByPlaceholderText('{"key":"value"}')).toHaveValue(
      JSON.stringify({ a: 1 }, null, 2),
    );
  });

  it("renders an enum as a select and submits the chosen string value", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: { input_schema: { properties: { priority: schema.input_schema.properties.priority }, required: [] } },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    await user.selectOptions(screen.getByRole("combobox"), "high");
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(onAnswer).toHaveBeenCalledWith({ inputs: { priority: "high" } });
  });

  it("blocks submit and shows an error when a required field is left blank", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "input", evidence: schema })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(await screen.findByText("to is required.")).toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("parses a valid integer value and submits it as a number", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { count: { type: "integer" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const input = screen.getByRole("spinbutton");
    await user.type(input, "42");
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(onAnswer).toHaveBeenCalledWith({ inputs: { count: 42 } });
  });

  it("parses a valid decimal value for a number field and submits it", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { score: { type: "number" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const input = screen.getByRole("spinbutton");
    await user.type(input, "0.85");
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(onAnswer).toHaveBeenCalledWith({ inputs: { score: 0.85 } });
  });

  it("rejects a non-integer value for an integer field", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { count: { type: "integer" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const input = screen.getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "3.5");
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(await screen.findByText("count must be an integer.")).toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("rejects a non-numeric value for a number field", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { score: { type: "number" } },
              required: ["score"],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const input = screen.getByRole("spinbutton");
    await user.type(input, "abc");
    // A non-numeric value in a number input renders empty in the browser too;
    // type it via the underlying textbox role instead to force a bad string.
    // (jsdom keeps type="number" inputs' value cleared for invalid text, so
    // required-empty is exercised here rather than the NaN branch directly.)
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(await screen.findByText("score is required.")).toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("rejects invalid JSON for an object/array field", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { payload: { type: "object" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const textarea = screen.getByPlaceholderText('{"key":"value"}');
    fireEvent.change(textarea, { target: { value: "{not valid json" } });
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(await screen.findByText("payload must be valid JSON.")).toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("parses valid JSON for an array field and submits it", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { tags: { type: "array" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const textarea = screen.getByPlaceholderText('["item"]');
    fireEvent.change(textarea, { target: { value: '["a","b"]' } });
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(onAnswer).toHaveBeenCalledWith({ inputs: { tags: ["a", "b"] } });
  });

  it("omits an optional blank field from the submitted inputs", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { to: { type: "string" }, cc: { type: "string" } },
              required: ["to"],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    const toInput = screen.getAllByRole("textbox")[0]!;
    await user.type(toInput, "a@b.com");
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(onAnswer).toHaveBeenCalledWith({ inputs: { to: "a@b.com" } });
  });

  it("toggles a boolean field via its checkbox and submits the boolean", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { notify: { type: "boolean" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Continue plan" }));
    expect(onAnswer).toHaveBeenCalledWith({ inputs: { notify: true } });
  });

  it("lets the user skip the step even mid-input", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "input", evidence: schema })}
        pending={false}
        onAnswer={onAnswer}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Skip step" }));
    expect(onAnswer).toHaveBeenCalledWith({ approved: false });
  });

  it("disables Continue plan when every input resolves from a prior step", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: { properties: { to: { type: "string" } }, required: ["to"] },
            current_inputs: { to: { $from_step: "STP-0" } },
          },
        })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Continue plan" })).toBeDisabled();
  });

  it("disables Continue plan and Skip step while pending", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({ kind: "input", evidence: schema })}
        pending
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Continue plan" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skip step" })).toBeDisabled();
  });

  it("shows the field description text when present", () => {
    render(
      <PlanInteractionPrompt
        interaction={makeInteraction({
          kind: "input",
          evidence: {
            input_schema: {
              properties: { payload: { type: "object", description: "Extra JSON payload" } },
              required: [],
            },
          },
        })}
        pending={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByText("Extra JSON payload")).toBeInTheDocument();
  });
});
