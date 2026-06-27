/**
 * Per-node "code" view/editor — opened from a node's `<>` toolbar button.
 *
 * CALIBER's runtime is a manifest-driven interpreter (not per-node `exec`), and
 * the manifest is the single source of truth. So a node's "code" is its
 * declarative manifest JSON: this modal shows that JSON and, on Apply, parses it
 * back into the manifest — exactly like the whole-workflow Code view, scoped to
 * one node. No new execution path is introduced, so it can't break runtime
 * behaviour or determinism. The node `id` is held fixed because edges reference
 * it; everything else (including `type`) flows through the normal validator.
 */

import { Code2, X } from "lucide-react";
import { useState } from "react";

import type { ManifestNode } from "@/api/workflowTypes";
import { CodeEditorField } from "@/components/workflows/CodeHighlight";

interface NodeCodeModalProps {
  node: ManifestNode;
  /** Apply the edited node back into the manifest (caller re-validates + re-renders). */
  onApply: (node: ManifestNode) => void;
  onClose: () => void;
}

export function NodeCodeModal({ node, onApply, onClose }: NodeCodeModalProps): JSX.Element {
  const [text, setText] = useState(() => JSON.stringify(node, null, 2));
  const [error, setError] = useState<string | null>(null);

  const apply = (): void => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid JSON");
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setError("Node code must be a JSON object.");
      return;
    }
    const candidate = parsed as Record<string, unknown>;
    if (typeof candidate.type !== "string" || !candidate.type) {
      setError('Node code must include a string "type".');
      return;
    }
    // The id is immutable here — edges reference it. Everything else (including
    // type) is handed to the normal manifest validation pipeline.
    onApply({ ...(candidate as ManifestNode), id: node.id });
    onClose();
  };

  return (
    <div
      data-testid="node-code-modal"
      className="absolute inset-0 z-30 flex items-center justify-center bg-zinc-900/40 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-100 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-md bg-zinc-100 text-zinc-600">
              <Code2 size={15} strokeWidth={2} aria-hidden />
            </span>
            <div>
              <div className="text-sm font-semibold text-zinc-900">
                <span className="font-mono">{node.id}</span> · code
              </div>
              <div className="text-[11px] text-zinc-500">
                Editing this node&apos;s manifest JSON — the workflow&apos;s source of truth.
              </div>
            </div>
          </div>
          <button
            type="button"
            data-testid="node-code-close"
            onClick={onClose}
            aria-label="Close"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700"
          >
            <X size={16} strokeWidth={2} aria-hidden />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <CodeEditorField
            testId="node-code-editor"
            ariaLabel={`${node.id} node code`}
            language="json"
            value={text}
            onChange={(next) => {
              setText(next);
              if (error) setError(null);
            }}
            className="h-[48vh] rounded-lg border border-zinc-200"
          />
          {error && (
            <div
              data-testid="node-code-error"
              className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
            >
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-zinc-100 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-semibold text-zinc-600 transition-colors hover:bg-zinc-50 hover:text-zinc-900"
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="node-code-apply"
            onClick={apply}
            className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-zinc-800"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}
