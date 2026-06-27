/**
 * Graph diff view — n8n-inspired change visualization.
 *
 * Shows added (emerald), removed (red), and modified (amber) nodes/edges plus
 * artifact and deploy-gate changes with clean monochromatic card styling.
 */

import type { GraphDiff as GraphDiffData } from "@/api/workflowTypes";

interface GraphDiffProps {
  diff: GraphDiffData;
}

export function GraphDiff({ diff }: GraphDiffProps): JSX.Element {
  if (diff.empty) {
    return (
      <div data-testid="wf-graph-diff" className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-sm text-zinc-500">
        No graph changes.
      </div>
    );
  }
  return (
    <div data-testid="wf-graph-diff" className="space-y-1.5 text-sm">
      {diff.added_nodes.map((n) => (
        <div key={`an-${n.id}`} data-testid="diff-added-node" className="flex items-center gap-2 rounded-md bg-emerald-50 px-2.5 py-1.5 text-emerald-700">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> + node {n.id}
          {n.type ? ` (${n.type})` : ""}
        </div>
      ))}
      {diff.removed_nodes.map((n) => (
        <div key={`rn-${n.id}`} data-testid="diff-removed-node" className="flex items-center gap-2 rounded-md bg-red-50 px-2.5 py-1.5 text-red-700">
          <span className="h-1.5 w-1.5 rounded-full bg-red-500" /> − node {n.id}
          {n.type ? ` (${n.type})` : ""}
        </div>
      ))}
      {diff.modified_nodes.map((n) => (
        <div key={`mn-${n.id}`} data-testid="diff-modified-node" className="flex items-center gap-2 rounded-md bg-amber-50 px-2.5 py-1.5 text-amber-700">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500" /> ~ node {n.id}: {n.changes.map((c) => c.field).join(", ")}
        </div>
      ))}
      {diff.added_edges.map((e) => (
        <div key={`ae-${e}`} data-testid="diff-added-edge" className="flex items-center gap-2 rounded-md bg-emerald-50 px-2.5 py-1.5 text-emerald-700">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> + edge {e}
        </div>
      ))}
      {diff.removed_edges.map((e) => (
        <div key={`re-${e}`} data-testid="diff-removed-edge" className="flex items-center gap-2 rounded-md bg-red-50 px-2.5 py-1.5 text-red-700">
          <span className="h-1.5 w-1.5 rounded-full bg-red-500" /> − edge {e}
        </div>
      ))}
      {diff.modified_edges.map((e) => (
        <div key={`me-${e.id}`} data-testid="diff-modified-edge" className="flex items-center gap-2 rounded-md bg-amber-50 px-2.5 py-1.5 text-amber-700">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500" /> ~ edge {e.id}
          {e.changes.length > 0 ? `: ${e.changes.map((c) => c.field).join(", ")}` : ""}
        </div>
      ))}
      {diff.artifact_changes.map((c, i) => (
        <div key={`ac-${i}`} data-testid="diff-artifact-change" className="flex items-center gap-2 rounded-md bg-caliber-50 px-2.5 py-1.5 text-caliber-700">
          <span className="h-1.5 w-1.5 rounded-full bg-caliber-500" /> ~ artifact {String((c as Record<string, unknown>).ref ?? (c as Record<string, unknown>).kind ?? "")}
        </div>
      ))}
      {diff.deploy_gate_changes.map((c, i) => (
        <div key={`dg-${i}`} data-testid="diff-gate-change" className="flex items-center gap-2 rounded-md bg-caliber-50 px-2.5 py-1.5 text-caliber-700">
          <span className="h-1.5 w-1.5 rounded-full bg-caliber-500" /> ~ deploy gate {String((c as Record<string, unknown>).name ?? "")}
        </div>
      ))}
    </div>
  );
}
