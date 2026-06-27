import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "@/components/CopyButton";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ``fireEvent.click`` (not ``userEvent``) is used deliberately: ``userEvent.setup()``
// installs its own ``navigator.clipboard`` stub, which would clobber the spy we
// assert against here.

describe("CopyButton", () => {
  it("writes the value to the clipboard and flashes confirmation", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    render(<CopyButton value="TL-abc123" label="Copy tool ID" testId="copy" />);

    fireEvent.click(screen.getByTestId("copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("TL-abc123"));
  });

  it("does not trigger an enclosing row's click handler", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const rowClick = vi.fn();

    render(
      <div onClick={rowClick}>
        <CopyButton value="x" testId="copy" />
      </div>,
    );

    fireEvent.click(screen.getByTestId("copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(rowClick).not.toHaveBeenCalled();
  });

  it("degrades gracefully when the Clipboard API is unavailable", () => {
    vi.stubGlobal("navigator", { clipboard: undefined });

    render(<CopyButton value="x" testId="copy" />);

    // Clicking must not throw even though there is nothing to copy with.
    fireEvent.click(screen.getByTestId("copy"));
    expect(screen.getByTestId("copy")).toBeInTheDocument();
  });
});
