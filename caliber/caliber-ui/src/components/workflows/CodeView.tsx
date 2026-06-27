/**
 * Visual ⇄ Code view for the Workflow Studio (Lakeflow "one artifact" pattern).
 *
 * - **Manifest** tab: editable JSON of the workflow manifest (the source of
 *   truth). "Apply" parses it and hands it back so the canvas re-renders —
 *   no-code and code edit the *same* artifact.
 * - **Python** tab: read-only generated Agents-SDK code (compiled server-side),
 *   loaded on demand. You inspect/operationalize the same versioned artifact.
 */

import { useEffect, useState } from "react";

import type { WorkflowManifest } from "@/api/workflowTypes";
import { CodeBlock, CodeEditorField } from "@/components/workflows/CodeHighlight";

interface CodeViewProps {
  manifest: WorkflowManifest;
  /** Apply edited manifest JSON back to the editor (re-renders the canvas). */
  onApplyManifest: (manifest: WorkflowManifest) => void;
  /** Lazily fetch the compiled Python (raw text). */
  loadPython: () => Promise<string>;
}

type PythonState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "loaded"; code: string }
  | { status: "error"; error: string };

function manifestToText(manifest: WorkflowManifest): string {
  return JSON.stringify(manifest, null, 2);
}

export function CodeView({ manifest, onApplyManifest, loadPython }: CodeViewProps): JSX.Element {
  const [tab, setTab] = useState<"manifest" | "python">("manifest");
  // Local editor buffer; re-keyed to the manifest below so external applies sync.
  const [text, setText] = useState(() => manifestToText(manifest));
  const [baseline, setBaseline] = useState(() => manifestToText(manifest));
  const [error, setError] = useState<string | null>(null);
  const [py, setPy] = useState<PythonState>({ status: "idle" });

  // Re-sync the buffer when the manifest changes from outside (Apply
  // canonicalizes it, or another edit lands) so the code view tracks the
  // source of truth.
  useEffect(() => {
    const synced = manifestToText(manifest);
    setText(synced);
    setBaseline(synced);
    setError(null);
  }, [manifest]);

  const dirty = text !== baseline;

  function apply(): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid JSON");
      return;
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as { nodes?: unknown }).nodes !== "object"
    ) {
      setError("Manifest must be a JSON object with a 'nodes' map.");
      return;
    }
    setError(null);
    onApplyManifest(parsed as WorkflowManifest);
  }

  function revert(): void {
    setText(baseline);
    setError(null);
  }

  function openPython(): void {
    setTab("python");
    if (py.status === "idle" || py.status === "error") {
      setPy({ status: "loading" });
      loadPython().then(
        (code) => setPy({ status: "loaded", code }),
        (e) => setPy({ status: "error", error: e instanceof Error ? e.message : "export failed" }),
      );
    }
  }

  return (
    <div data-testid="code-view" className="flex h-full flex-col bg-white">
      <div className="flex items-center gap-1 border-b border-zinc-200 px-3 py-2">
        <button
          type="button"
          data-testid="code-tab-manifest"
          onClick={() => setTab("manifest")}
          className={`rounded-lg px-3 py-1 text-xs font-medium transition-colors ${
            tab === "manifest" ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"
          }`}
        >
          Manifest
        </button>
        <button
          type="button"
          data-testid="code-tab-python"
          onClick={openPython}
          className={`rounded-lg px-3 py-1 text-xs font-medium transition-colors ${
            tab === "python" ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-100"
          }`}
        >
          Python
        </button>
        <span className="ml-auto text-[11px] text-zinc-400">
          {tab === "manifest" ? "Editable — the manifest is the source of truth" : "Read-only — compiled artifact"}
        </span>
      </div>

      {tab === "manifest" ? (
        <div className="flex min-h-0 flex-1 flex-col p-3">
          <CodeEditorField
            testId="code-manifest-editor"
            ariaLabel="Workflow manifest JSON"
            language="json"
            value={text}
            onChange={setText}
            className="min-h-0 flex-1 rounded-lg border border-zinc-200 bg-zinc-50 focus-within:border-zinc-900 focus-within:ring-1 focus-within:ring-zinc-900"
          />
          {error && (
            <div data-testid="code-error" className="mt-2 rounded-lg border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-700">
              {error}
            </div>
          )}
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              data-testid="code-apply"
              disabled={!dirty}
              onClick={apply}
              className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
            >
              Apply to canvas
            </button>
            <button
              type="button"
              data-testid="code-revert"
              disabled={!dirty}
              onClick={revert}
              className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
            >
              Revert
            </button>
            {dirty && <span className="text-[11px] text-amber-600">Unapplied edits</span>}
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col p-3">
          {py.status === "loading" && <div className="text-xs text-zinc-400">Compiling…</div>}
          {py.status === "error" && (
            <div data-testid="code-python-error" className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-700">
              {py.error}
            </div>
          )}
          {py.status === "loaded" && (
            <CodeBlock
              testId="code-python"
              language="python"
              code={py.code}
              className="min-h-0 flex-1 overflow-auto whitespace-pre rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-zinc-800"
            />
          )}
        </div>
      )}
    </div>
  );
}
