import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ViewToggle } from "@/components/ViewToggle";

describe("ViewToggle", () => {
  it("marks the active segment with aria-pressed", () => {
    render(<ViewToggle value="grid" onChange={() => {}} />);

    expect(screen.getByTestId("view-toggle")).toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-grid")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("view-toggle-list")).toHaveAttribute("aria-pressed", "false");
  });

  it("fires onChange with the chosen mode", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ViewToggle value="grid" onChange={onChange} />);

    await user.click(screen.getByTestId("view-toggle-list"));
    expect(onChange).toHaveBeenCalledWith("list");

    await user.click(screen.getByTestId("view-toggle-grid"));
    expect(onChange).toHaveBeenCalledWith("grid");
  });

  it("reflects a list selection", () => {
    render(<ViewToggle value="list" onChange={() => {}} />);

    expect(screen.getByTestId("view-toggle-list")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("view-toggle-grid")).toHaveAttribute("aria-pressed", "false");
  });
});
