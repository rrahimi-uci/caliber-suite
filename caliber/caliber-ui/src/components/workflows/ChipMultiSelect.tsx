/**
 * Chip-based multi-select: selected items render as removable chips, and an
 * "＋ Add" control opens an inline, searchable list of the remaining options.
 *
 * Replaces long checkbox walls (which don't scale past a handful of items) for
 * picking an agent's tools / skills. Inline (not a popover) so it behaves well
 * inside a narrow, scrollable inspector column.
 */

import { useMemo, useState, type ReactNode } from "react";

export interface ChipOption {
  value: string;
  label: string;
  /** Secondary text shown in the add list (e.g. a skill summary). */
  hint?: string;
  /** Small trailing badge (e.g. side-effect level, "MCP"). */
  badge?: ReactNode;
  /** Explicit test id for the add-list row (defaults to ``<prefix>-option-<value>``). */
  testId?: string;
}

interface ChipMultiSelectProps {
  /** Test-id / a11y prefix, e.g. ``"tools"``. */
  prefix: string;
  options: ChipOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  /** Label on the add control, e.g. ``"Add tool"``. */
  addLabel?: string;
  /** Shown when there are no options to choose from at all. */
  emptyText?: string;
  searchPlaceholder?: string;
}

export function ChipMultiSelect({
  prefix,
  options,
  selected,
  onChange,
  addLabel = "Add",
  emptyText = "Nothing available.",
  searchPlaceholder = "Search…",
}: ChipMultiSelectProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const byValue = useMemo(() => {
    const map: Record<string, ChipOption> = {};
    for (const o of options) map[o.value] = o;
    return map;
  }, [options]);

  const available = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options.filter(
      (o) =>
        !selected.includes(o.value) &&
        (!q || o.label.toLowerCase().includes(q) || (o.hint ?? "").toLowerCase().includes(q)),
    );
  }, [options, selected, query]);

  function add(value: string): void {
    if (selected.includes(value)) return;
    onChange([...selected, value]);
  }
  function remove(value: string): void {
    onChange(selected.filter((v) => v !== value));
  }

  return (
    <div data-testid={prefix} className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {selected.length === 0 && (
          <span className="text-xs text-zinc-400">None selected</span>
        )}
        {selected.map((value) => {
          const opt = byValue[value];
          return (
            <span
              key={value}
              data-testid={`${prefix}-chip-${value}`}
              className="inline-flex items-center gap-1 rounded-full border border-caliber-200 bg-caliber-50 py-0.5 pl-2 pr-1 text-[11px] font-medium text-zinc-800"
            >
              {opt?.badge}
              <span className="max-w-[10rem] truncate">{opt?.label ?? value}</span>
              <button
                type="button"
                aria-label={`Remove ${opt?.label ?? value}`}
                data-testid={`${prefix}-remove-${value}`}
                onClick={() => remove(value)}
                className="flex h-4 w-4 items-center justify-center rounded-full text-zinc-400 transition-colors hover:bg-zinc-200 hover:text-zinc-700"
              >
                ✕
              </button>
            </span>
          );
        })}
        <button
          type="button"
          data-testid={`${prefix}-add`}
          aria-expanded={open}
          onClick={() => {
            setOpen((v) => !v);
            setQuery("");
          }}
          className={`inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-[11px] font-medium transition-colors ${
            open
              ? "border-zinc-400 bg-zinc-100 text-zinc-700"
              : "border-zinc-300 text-zinc-500 hover:border-zinc-400 hover:text-zinc-700"
          }`}
        >
          ＋ {addLabel}
        </button>
      </div>

      {open && (
        <div className="rounded-lg border border-zinc-200 bg-white p-2 shadow-sm">
          {options.length === 0 ? (
            <div className="px-1 py-1.5 text-xs text-zinc-400">{emptyText}</div>
          ) : (
            <>
              <input
                data-testid={`${prefix}-search`}
                aria-label={searchPlaceholder}
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setOpen(false);
                }}
                placeholder={searchPlaceholder}
                className="mb-1.5 w-full rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1 text-xs text-zinc-800 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
              />
              <div className="max-h-48 space-y-0.5 overflow-auto">
                {available.length === 0 && (
                  <div className="px-1 py-1.5 text-xs text-zinc-400">
                    {query ? "No matches." : "All added."}
                  </div>
                )}
                {available.map((o) => (
                  <button
                    key={o.value}
                    type="button"
                    data-testid={o.testId ?? `${prefix}-option-${o.value}`}
                    onClick={() => add(o.value)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-zinc-50"
                  >
                    <span className="font-medium text-zinc-900">{o.label}</span>
                    {o.badge}
                    {o.hint && <span className="truncate text-[11px] text-zinc-400">{o.hint}</span>}
                    <span className="ml-auto text-zinc-300">＋</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
