/**
 * Tests for the Visual ⇄ Code view (Lakeflow "one artifact" pattern):
 * editable manifest that round-trips to the canvas, read-only compiled Python.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkflowManifest } from "@/api/workflowTypes";
import { CodeView } from "@/components/workflows/CodeView";

const MANIFEST = {
  schema_version: 1,
  workflow_id: "wf",
  name: "WF",
  nodes: { start: { id: "start", type: "start" } },
  edges: [],
} as unknown as WorkflowManifest;

afterEach(() => vi.clearAllMocks());

function setup(over: { loadPython?: () => Promise<string> } = {}) {
  const onApply = vi.fn();
  const loadPython = over.loadPython ?? vi.fn().mockResolvedValue("# generated\nprint('hi')");
  render(<CodeView manifest={MANIFEST} onApplyManifest={onApply} loadPython={loadPython} />);
  return { onApply, loadPython };
}

const editor = () => screen.getByTestId("code-manifest-editor") as HTMLTextAreaElement;

describe("CodeView — manifest tab", () => {
  it("renders the manifest as editable JSON", () => {
    setup();
    expect(editor().value).toContain('"workflow_id": "wf"');
    // Not dirty on load → Apply disabled.
    expect(screen.getByTestId("code-apply")).toBeDisabled();
  });

  it("applies valid edited JSON back to the canvas", () => {
    const { onApply } = setup();
    const edited = JSON.stringify({ ...JSON.parse(editor().value), name: "Renamed" }, null, 2);
    fireEvent.change(editor(), { target: { value: edited } });
    fireEvent.click(screen.getByTestId("code-apply"));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply.mock.calls[0]![0]).toMatchObject({ name: "Renamed", workflow_id: "wf" });
  });

  it("shows an error and does not apply invalid JSON", () => {
    const { onApply } = setup();
    fireEvent.change(editor(), { target: { value: "{ not json" } });
    fireEvent.click(screen.getByTestId("code-apply"));
    expect(screen.getByTestId("code-error")).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("rejects a manifest without a nodes map", () => {
    const { onApply } = setup();
    fireEvent.change(editor(), { target: { value: JSON.stringify({ workflow_id: "x" }) } });
    fireEvent.click(screen.getByTestId("code-apply"));
    expect(screen.getByTestId("code-error")).toHaveTextContent("nodes");
    expect(onApply).not.toHaveBeenCalled();
  });

  it("reverts unapplied edits", () => {
    setup();
    const original = editor().value;
    fireEvent.change(editor(), { target: { value: `${original}\n// junk` } });
    expect(screen.getByTestId("code-apply")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("code-revert"));
    expect(editor().value).toBe(original);
  });
});

describe("CodeView — python tab", () => {
  it("lazy-loads and renders the compiled python", async () => {
    const loadPython = vi.fn().mockResolvedValue("# generated\nprint('hi')");
    setup({ loadPython });
    fireEvent.click(screen.getByTestId("code-tab-python"));
    expect(loadPython).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("code-python")).toHaveTextContent("print('hi')");
  });

  it("surfaces a compile/export error", async () => {
    const loadPython = vi.fn().mockRejectedValue(new Error("cannot export: boom"));
    setup({ loadPython });
    fireEvent.click(screen.getByTestId("code-tab-python"));
    expect(await screen.findByTestId("code-python-error")).toHaveTextContent("cannot export: boom");
  });
});
