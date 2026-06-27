/**
 * Tests for the syntax-highlighted code surfaces (CodeBlock + CodeEditorField).
 *
 * Focus: the editor's scroll-sync (the mirror <pre> must follow the textarea's
 * scroll position so the colored layer stays under the caret), plus the basic
 * highlighted-render paths for both surfaces.
 */

import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CodeBlock, CodeEditorField } from "@/components/workflows/CodeHighlight";
import { render, screen } from "@/test/utils";

describe("CodeBlock", () => {
  it("renders highlighted JSON with the surface test id", () => {
    render(<CodeBlock code={'{"a": 1}'} language="json" testId="cb" />);
    const block = screen.getByTestId("cb");
    expect(block).toHaveTextContent('{"a": 1}');
    // The key token gets its own colored span.
    expect(block.querySelector(".text-sky-700")).not.toBeNull();
  });
});

describe("CodeEditorField — scroll sync", () => {
  it("mirrors the textarea scroll position onto the highlight layer", () => {
    render(
      <CodeEditorField
        value={'{"k": "v"}'}
        onChange={vi.fn()}
        language="json"
        testId="editor"
        ariaLabel="Manifest editor"
      />,
    );

    const textarea = screen.getByTestId("editor") as HTMLTextAreaElement;
    const mirror = textarea.parentElement?.querySelector("pre") as HTMLPreElement;
    expect(mirror).not.toBeNull();

    // jsdom doesn't lay out, so scroll offsets are plain writable props.
    textarea.scrollTop = 42;
    textarea.scrollLeft = 7;
    fireEvent.scroll(textarea);

    expect(mirror.scrollTop).toBe(42);
    expect(mirror.scrollLeft).toBe(7);
  });

  it("propagates edits through onChange", () => {
    const onChange = vi.fn();
    render(
      <CodeEditorField
        value="x = 1"
        onChange={onChange}
        language="python"
        testId="editor"
        ariaLabel="Python editor"
      />,
    );

    const textarea = screen.getByLabelText("Python editor") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "y = 2" } });
    expect(onChange).toHaveBeenCalledWith("y = 2");
  });
});
