import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";

describe("ConfirmDialog", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <ConfirmDialog open={false} title="t" onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("confirms with an empty reason by default", async () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog open title="Roll back?" onConfirm={onConfirm} onCancel={vi.fn()} />);
    await userEvent.click(screen.getByTestId("confirm-dialog-confirm"));
    expect(onConfirm).toHaveBeenCalledWith("");
  });

  it("cancel fires onCancel", async () => {
    const onCancel = vi.fn();
    render(<ConfirmDialog open title="t" onConfirm={vi.fn()} onCancel={onCancel} />);
    await userEvent.click(screen.getByTestId("confirm-dialog-cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("requires a reason before confirming when requireReason is set", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Promote anyway?"
        requireReason
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const confirm = screen.getByTestId("confirm-dialog-confirm");
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByTestId("confirm-dialog-reason"), "  urgent hotfix  ");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("urgent hotfix"); // trimmed
  });
});
