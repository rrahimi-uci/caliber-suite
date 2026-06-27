import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ManifestNode } from "@/api/workflowTypes";
import { NodeCodeModal } from "@/components/workflows/NodeCodeModal";

const NODE: ManifestNode = {
  id: "hook",
  type: "api_request",
  mode: "url",
  url: "https://api.test/v1",
  method: "GET",
};

function setText(value: string): void {
  fireEvent.change(screen.getByTestId("node-code-editor"), { target: { value } });
}

describe("NodeCodeModal", () => {
  it("shows the node's manifest JSON as editable code", () => {
    render(<NodeCodeModal node={NODE} onApply={vi.fn()} onClose={vi.fn()} />);
    const editor = screen.getByTestId("node-code-editor") as HTMLTextAreaElement;
    expect(editor.value).toContain('"type": "api_request"');
    expect(editor.value).toContain('"url": "https://api.test/v1"');
  });

  it("applies edited JSON back to the manifest and closes", () => {
    const onApply = vi.fn();
    const onClose = vi.fn();
    render(<NodeCodeModal node={NODE} onApply={onApply} onClose={onClose} />);
    setText(JSON.stringify({ ...NODE, method: "POST" }, null, 2));
    fireEvent.click(screen.getByTestId("node-code-apply"));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply.mock.calls[0][0]).toMatchObject({ id: "hook", method: "POST" });
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the node id immutable even if edited in the JSON", () => {
    const onApply = vi.fn();
    render(<NodeCodeModal node={NODE} onApply={onApply} onClose={vi.fn()} />);
    setText(JSON.stringify({ ...NODE, id: "renamed" }, null, 2));
    fireEvent.click(screen.getByTestId("node-code-apply"));
    expect(onApply.mock.calls[0][0].id).toBe("hook");
  });

  it("rejects invalid JSON without applying", () => {
    const onApply = vi.fn();
    const onClose = vi.fn();
    render(<NodeCodeModal node={NODE} onApply={onApply} onClose={onClose} />);
    setText("{ not valid json ");
    fireEvent.click(screen.getByTestId("node-code-apply"));
    expect(screen.getByTestId("node-code-error")).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("rejects a node without a type", () => {
    const onApply = vi.fn();
    render(<NodeCodeModal node={NODE} onApply={onApply} onClose={vi.fn()} />);
    setText(JSON.stringify({ id: "hook", url: "x" }, null, 2));
    fireEvent.click(screen.getByTestId("node-code-apply"));
    expect(screen.getByTestId("node-code-error")).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("closes via the close button", () => {
    const onClose = vi.fn();
    render(<NodeCodeModal node={NODE} onApply={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("node-code-close"));
    expect(onClose).toHaveBeenCalled();
  });
});
