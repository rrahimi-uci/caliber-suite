import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ListRow } from "@/components/ListRow";

describe("ListRow keyboard accessibility", () => {
  it("exposes a clickable row as a focusable button and activates on Enter/Space", () => {
    const onClick = vi.fn();
    render(<ListRow title="Item" onClick={onClick} testId="row" />);

    const row = screen.getByTestId("row");
    expect(row).toHaveAttribute("role", "button");
    expect(row).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(row, { key: "Enter" });
    expect(onClick).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(row, { key: " " });
    expect(onClick).toHaveBeenCalledTimes(2);

    // An unrelated key does nothing.
    fireEvent.keyDown(row, { key: "a" });
    expect(onClick).toHaveBeenCalledTimes(2);
  });

  it("leaves a non-interactive row as a plain element (no role/tabindex)", () => {
    render(<ListRow title="Static" testId="static-row" />);

    const row = screen.getByTestId("static-row");
    expect(row).not.toHaveAttribute("role");
    expect(row).not.toHaveAttribute("tabindex");
  });
});
