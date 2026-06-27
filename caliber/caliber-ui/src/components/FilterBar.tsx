import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface FilterBarProps {
  /** The page's `<SearchInput>` (pass it `className="w-full"` — the bar sizes it). */
  search?: ReactNode;
  /** Filter controls (`<FilterSelect>`s, status chips, etc.), laid out inline. */
  filters?: ReactNode;
  /** Right-aligned cluster: Clear-filters, ViewToggle, and any page actions. */
  actions?: ReactNode;
  className?: string;
}

/**
 * FilterBar — the one toolbar layout shared by every list page (search box +
 * filter dropdowns + grid/list toggle + clear). It exists to kill the drift
 * that crept in when each page wired its own row: inconsistent search widths,
 * mixed `sm`/`lg`/`xl` breakpoints, and `ml-auto` on the right cluster that left
 * a big empty gap between the filters and the view toggle.
 *
 * Layout: one wrapping flex row inside a card. The search slot **grows** to
 * absorb slack (`flex-1`) so the filter + action controls sit together with no
 * dead space, instead of being shoved to the far edge. Everything wraps cleanly
 * on narrow screens. Filters and actions keep a consistent `gap-2`.
 */
export function FilterBar({
  search,
  filters,
  actions,
  className,
}: FilterBarProps): JSX.Element {
  return (
    <div
      data-testid="filter-bar"
      className={cn("card flex flex-wrap items-center gap-2 p-3", className)}
    >
      {search ? <div className="min-w-[280px] flex-1 sm:min-w-[360px]">{search}</div> : null}
      {filters ? (
        <div className="flex flex-wrap items-center gap-2">{filters}</div>
      ) : null}
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}
