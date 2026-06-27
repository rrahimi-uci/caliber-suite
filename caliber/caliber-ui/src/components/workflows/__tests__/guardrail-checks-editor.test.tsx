/**
 * Tests for the no-code guardrail checks editor (P3). Each check round-trips to
 * the manifest as a single-key `{ <kind>: <params> }` object, matching the
 * backend's closed check vocabulary.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GuardrailChecksEditor } from "@/components/workflows/GuardrailChecksEditor";

afterEach(() => vi.clearAllMocks());

type Check = Record<string, unknown>;

function setup(checks: Check[] = []) {
  const onChange = vi.fn();
  const { rerender } = render(<GuardrailChecksEditor checks={checks} onChange={onChange} />);
  // Re-render with whatever onChange was last called with, so we can assert the
  // *rendered* result of an edit (parent owns state in real usage).
  const apply = () => {
    const next = onChange.mock.calls.at(-1)?.[0] as Check[];
    rerender(<GuardrailChecksEditor checks={next} onChange={onChange} />);
    return next;
  };
  return { onChange, apply };
}

describe("GuardrailChecksEditor", () => {
  it("shows an empty-state when there are no checks", () => {
    setup([]);
    expect(screen.getByText(/no checks yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId("check-row-0")).not.toBeInTheDocument();
  });

  it("renders an existing check with its human label and params", () => {
    setup([{ forbid_substring: { substring: "secret" } }]);
    const row = screen.getByTestId("check-row-0");
    expect(within(row).getByText("Forbid substring")).toBeInTheDocument();
    expect(screen.getByTestId("check-0-param-substring")).toHaveValue("secret");
  });

  it("adds the selected check kind as a single-key object", () => {
    const { onChange } = setup([]);
    fireEvent.change(screen.getByTestId("check-add-kind"), { target: { value: "max_length" } });
    fireEvent.click(screen.getByTestId("check-add"));
    expect(onChange).toHaveBeenCalledWith([{ max_length: {} }]);
  });

  it("removes a check by index", () => {
    const { onChange } = setup([{ non_empty_output: {} }, { max_length: { max_chars: 10 } }]);
    fireEvent.click(screen.getByTestId("check-remove-0"));
    expect(onChange).toHaveBeenCalledWith([{ max_length: { max_chars: 10 } }]);
  });

  it("edits a string param", () => {
    const { onChange } = setup([{ forbid_substring: {} }]);
    fireEvent.change(screen.getByTestId("check-0-param-substring"), { target: { value: "topsecret" } });
    expect(onChange).toHaveBeenCalledWith([{ forbid_substring: { substring: "topsecret" } }]);
  });

  it("stores a number param as a number, not a string", () => {
    const { onChange } = setup([{ max_length: {} }]);
    fireEvent.change(screen.getByTestId("check-0-param-max_chars"), { target: { value: "2048" } });
    expect(onChange).toHaveBeenCalledWith([{ max_length: { max_chars: 2048 } }]);
    expect(typeof (onChange.mock.calls[0]![0][0] as Check).max_length).toBe("object");
    expect(((onChange.mock.calls[0]![0][0] as Check).max_length as { max_chars: unknown }).max_chars).toBe(2048);
  });

  it("clears a number param to empty string when blanked", () => {
    const { onChange } = setup([{ budget_limit: { max_usd: 5 } }]);
    fireEvent.change(screen.getByTestId("check-0-param-max_usd"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith([{ budget_limit: { max_usd: "" } }]);
  });

  it("splits a csv param into a trimmed array", () => {
    const { onChange } = setup([{ schema_validation: {} }]);
    fireEvent.change(screen.getByTestId("check-0-param-required_fields"), {
      target: { value: "id, name ,  email " },
    });
    expect(onChange).toHaveBeenCalledWith([
      { schema_validation: { required_fields: ["id", "name", "email"] } },
    ]);
  });

  it("renders a csv param array back as a comma-joined string", () => {
    setup([{ schema_validation: { required_fields: ["id", "name"] } }]);
    expect(screen.getByTestId("check-0-param-required_fields")).toHaveValue("id, name");
  });

  it("toggles a PII entity on and off", () => {
    const { onChange, apply } = setup([{ pii_detection: { entities: ["email"] } }]);
    // email starts checked; ssn unchecked.
    expect(screen.getByTestId("check-0-entity-email")).toBeChecked();
    expect(screen.getByTestId("check-0-entity-ssn")).not.toBeChecked();

    fireEvent.click(screen.getByTestId("check-0-entity-ssn"));
    expect(onChange).toHaveBeenLastCalledWith([{ pii_detection: { entities: ["email", "ssn"] } }]);
    apply();

    fireEvent.click(screen.getByTestId("check-0-entity-email"));
    expect(onChange).toHaveBeenLastCalledWith([{ pii_detection: { entities: ["ssn"] } }]);
  });

  it("renders a param-less check as 'No configuration.'", () => {
    setup([{ non_empty_output: {} }]);
    const row = screen.getByTestId("check-row-0");
    expect(within(row).getByText(/no configuration/i)).toBeInTheDocument();
  });

  it("reads the canonical {kind,params} shape too (e.g. imported workflows)", () => {
    // manifest.to_dict() emits this flat form; the editor must still render it.
    setup([{ kind: "max_length", params: { max_chars: 512 } }]);
    const row = screen.getByTestId("check-row-0");
    expect(within(row).getByText("Max length")).toBeInTheDocument();
    expect(screen.getByTestId("check-0-param-max_chars")).toHaveValue(512);
  });

  it("normalizes a canonical check to single-key sugar on edit", () => {
    const { onChange } = setup([{ kind: "forbid_substring", params: { substring: "old" } }]);
    fireEvent.change(screen.getByTestId("check-0-param-substring"), { target: { value: "new" } });
    expect(onChange).toHaveBeenCalledWith([{ forbid_substring: { substring: "new" } }]);
  });

  it("offers all eight check kinds in the add dropdown", () => {
    setup([]);
    const select = screen.getByTestId("check-add-kind");
    expect(within(select).getAllByRole("option")).toHaveLength(8);
  });
});
