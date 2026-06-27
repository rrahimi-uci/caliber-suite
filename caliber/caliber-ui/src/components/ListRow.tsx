/**
 * ListRow — the light shared shell for the **list** layout that card-based
 * pages drop into when {@link ViewToggle} is flipped from grid to list. It is
 * deliberately thin: a clickable, hover-highlighted row with a leading icon,
 * a primary/secondary title block, a flexible columns slot, and a trailing
 * actions slot. Each page supplies its own columns/actions (they differ per
 * page); this component only owns the row chrome so every list view feels the
 * same. A heavier generic table abstraction is intentionally avoided.
 *
 * ``ListRows`` is the matching vertically-stacked container.
 */

import type { KeyboardEvent, ReactNode } from "react";

export interface ListRowsProps {
  children: ReactNode;
  testId?: string;
  className?: string;
}

/** Vertically stacked container for {@link ListRow}s, with hairline dividers. */
export function ListRows({ children, testId, className = "" }: ListRowsProps): JSX.Element {
  return (
    <div
      data-testid={testId}
      className={`divide-y divide-slate-100 overflow-hidden rounded-2xl border border-slate-200/60 bg-white shadow-card dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-950 ${className}`}
    >
      {children}
    </div>
  );
}

export interface ListRowProps {
  /** Leading visual (icon/avatar). */
  icon?: ReactNode;
  /** Primary line (item name). */
  title: ReactNode;
  /** Optional muted second line under the title. */
  subtitle?: ReactNode;
  /** Flexible middle columns (status, metrics, owner, version…). */
  columns?: ReactNode;
  /** Trailing actions (Open, edit, delete…). */
  actions?: ReactNode;
  /** Row click — typically opens the item, mirroring the card. */
  onClick?: () => void;
  title_attr?: string;
  testId?: string;
  className?: string;
}

export function ListRow({
  icon,
  title,
  subtitle,
  columns,
  actions,
  onClick,
  title_attr,
  testId,
  className = "",
}: ListRowProps): JSX.Element {
  const clickable = typeof onClick === "function";
  // A clickable row must also be reachable + activatable by keyboard
  // (Enter/Space) and announced as a button to assistive tech — without these
  // the row was a mouse-only target. Bundled as conditional props so a
  // non-interactive row stays a plain <div> (no spurious role/tabIndex).
  const interactiveProps = clickable
    ? {
        role: "button" as const,
        tabIndex: 0,
        onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onClick?.();
          }
        },
      }
    : {};
  return (
    <div
      data-testid={testId}
      title={title_attr}
      onClick={onClick}
      {...interactiveProps}
      className={`group flex items-center gap-3 px-4 py-3 transition-colors ${
        clickable
          ? "cursor-pointer hover:bg-slate-50 focus:outline-none focus:ring-1 focus:ring-inset focus:ring-caliber-500/30 dark:hover:bg-slate-900/60"
          : ""
      } ${className}`}
    >
      {icon && <div className="flex-shrink-0">{icon}</div>}
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
          {title}
        </div>
        {subtitle && (
          <div className="mt-0.5 truncate text-xs text-slate-500 dark:text-slate-400">
            {subtitle}
          </div>
        )}
      </div>
      {columns && (
        <div className="hidden flex-shrink-0 items-center gap-6 text-xs text-slate-500 dark:text-slate-400 sm:flex">
          {columns}
        </div>
      )}
      {actions && <div className="flex flex-shrink-0 items-center gap-1">{actions}</div>}
    </div>
  );
}
