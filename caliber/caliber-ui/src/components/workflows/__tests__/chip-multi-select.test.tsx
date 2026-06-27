/** Tests for the chips + searchable-add multi-select (replaces checkbox walls). */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChipMultiSelect } from "@/components/workflows/ChipMultiSelect";

afterEach(() => vi.clearAllMocks());

const OPTIONS = [
  { value: "lookup_policy", label: "lookup_policy" },
  { value: "get_order", label: "get_order" },
  { value: "escalate", label: "escalate", hint: "external action" },
];

function setup(selected: string[] = []) {
  const onChange = vi.fn();
  render(
    <ChipMultiSelect prefix="tools" options={OPTIONS} selected={selected} onChange={onChange} addLabel="Add tool" />,
  );
  return { onChange };
}

describe("ChipMultiSelect", () => {
  it("shows an empty state and no chips when nothing is selected", () => {
    setup([]);
    expect(screen.getByText(/none selected/i)).toBeInTheDocument();
    expect(screen.queryByTestId("tools-chip-lookup_policy")).not.toBeInTheDocument();
    // The add list is closed until opened.
    expect(screen.queryByTestId("tools-search")).not.toBeInTheDocument();
  });

  it("renders selected items as chips", () => {
    setup(["get_order"]);
    expect(screen.getByTestId("tools-chip-get_order")).toHaveTextContent("get_order");
  });

  it("adds an option from the searchable list", async () => {
    const { onChange } = setup(["get_order"]);
    await userEvent.click(screen.getByTestId("tools-add"));
    // Already-selected options are not offered.
    expect(screen.queryByTestId("tool-option-get_order")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("tools-option-lookup_policy"));
    expect(onChange).toHaveBeenCalledWith(["get_order", "lookup_policy"]);
  });

  it("filters the add list by search", async () => {
    setup([]);
    await userEvent.click(screen.getByTestId("tools-add"));
    await userEvent.type(screen.getByTestId("tools-search"), "esc");
    expect(screen.getByTestId("tools-option-escalate")).toBeInTheDocument();
    expect(screen.queryByTestId("tools-option-lookup_policy")).not.toBeInTheDocument();
  });

  it("removes a chip", async () => {
    const { onChange } = setup(["lookup_policy", "get_order"]);
    await userEvent.click(screen.getByTestId("tools-remove-lookup_policy"));
    expect(onChange).toHaveBeenCalledWith(["get_order"]);
  });

  it("honors a custom option testId", async () => {
    const onChange = vi.fn();
    render(
      <ChipMultiSelect
        prefix="tools"
        options={[{ value: "lookup_policy", label: "lookup_policy", testId: "tool-lookup_policy" }]}
        selected={[]}
        onChange={onChange}
      />,
    );
    await userEvent.click(screen.getByTestId("tools-add"));
    await userEvent.click(screen.getByTestId("tool-lookup_policy"));
    expect(onChange).toHaveBeenCalledWith(["lookup_policy"]);
  });
});
