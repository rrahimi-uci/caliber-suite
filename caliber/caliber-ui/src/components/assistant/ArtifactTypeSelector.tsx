/* ArtifactTypeSelector — radio-button selector for the five artifact types. */

import { cn } from "@/lib/utils";
import type { ArtifactType } from "@/api/assistantTypes";
import { ARTIFACT_TYPE_LABELS, ARTIFACT_TYPES } from "@/api/assistantTypes";

const TYPE_ICONS: Record<ArtifactType, string> = {
  tool: "🔧",
  skill: "⚡",
  prompt: "📝",
  workflow: "🔀",
  mcp_server: "🖥️",
};

interface Props {
  value: ArtifactType | null;
  onChange: (type: ArtifactType) => void;
  className?: string;
}

export function ArtifactTypeSelector({
  value,
  onChange,
  className,
}: Props): JSX.Element {
  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {ARTIFACT_TYPES.map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => onChange(t)}
          className={cn(
            "flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm font-medium transition-colors",
            value === t
              ? "border-caliber-500 bg-caliber-50 text-caliber-700 ring-1 ring-caliber-500/30"
              : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50",
          )}
        >
          <span>{TYPE_ICONS[t]}</span>
          <span>{ARTIFACT_TYPE_LABELS[t]}</span>
        </button>
      ))}
    </div>
  );
}
