/**
 * ViewToggle — a small two-option segmented control that flips a card-based
 * list page between its **grid** (cards) and **list** (rows) layouts. The
 * companion to {@link useViewMode}, which persists the choice. Styled to sit
 * in the same filter toolbar as {@link FilterSelect} / {@link SearchInput}: a
 * single rounded, bordered shell with the active segment filled in the brand
 * caliber tint.
 *
 * Accessibility: rendered as a ``role="group"`` of two ``aria-pressed``
 * buttons (a toolbar toggle group) with explicit labels, so screen readers
 * announce which layout is active.
 */

import { LayoutGrid, Rows3 } from "lucide-react";

import type { ViewMode } from "@/hooks/useViewMode";

export interface ViewToggleProps {
  value: ViewMode;
  onChange: (value: ViewMode) => void;
  className?: string;
}

const SEGMENT_BASE =
  "inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-caliber-500/40";
const SEGMENT_ACTIVE =
  "bg-caliber-50 text-caliber-700 shadow-sm dark:bg-caliber-500/15 dark:text-caliber-200";
const SEGMENT_INACTIVE =
  "text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200";

export function ViewToggle({ value, onChange, className = "" }: ViewToggleProps): JSX.Element {
  return (
    <div
      role="group"
      aria-label="View layout"
      data-testid="view-toggle"
      className={`inline-flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 dark:border-slate-700/70 dark:bg-slate-950 ${className}`}
    >
      <button
        type="button"
        data-testid="view-toggle-grid"
        aria-label="Grid view"
        aria-pressed={value === "grid"}
        onClick={() => onChange("grid")}
        className={`${SEGMENT_BASE} ${value === "grid" ? SEGMENT_ACTIVE : SEGMENT_INACTIVE}`}
      >
        <LayoutGrid className="h-4 w-4" aria-hidden="true" />
      </button>
      <button
        type="button"
        data-testid="view-toggle-list"
        aria-label="List view"
        aria-pressed={value === "list"}
        onClick={() => onChange("list")}
        className={`${SEGMENT_BASE} ${value === "list" ? SEGMENT_ACTIVE : SEGMENT_INACTIVE}`}
      >
        <Rows3 className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}
