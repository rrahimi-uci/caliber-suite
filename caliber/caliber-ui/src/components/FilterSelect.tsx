/**
 * FilterSelect — the shared dropdown filter for list pages, the companion to
 * {@link SearchInput}. A labelled single-select styled to match the search box
 * so a page's filter toolbar reads as one unit. The empty-string value is the
 * "no filter" sentinel and renders the leading "All …" option, so a page can
 * treat `""` as "show everything".
 */

export interface FilterSelectOption {
  value: string;
  label: string;
}

export interface FilterSelectProps {
  /** Current selection; `""` means no filter (the "All" option). */
  value: string;
  onChange: (value: string) => void;
  options: FilterSelectOption[];
  /** Field name, e.g. "Status" — drives the default "All status" option + aria. */
  label: string;
  /** Override the leading option label (default `All ${label.toLowerCase()}`). */
  allLabel?: string;
  className?: string;
}

export function FilterSelect({
  value,
  onChange,
  options,
  label,
  allLabel,
  className = "",
}: FilterSelectProps): JSX.Element {
  return (
    <div className={`relative ${className}`}>
      <select
        aria-label={`Filter by ${label.toLowerCase()}`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full appearance-none rounded-lg border border-slate-200 bg-white py-2 pl-3 pr-9 text-sm text-slate-700 transition-colors focus:border-caliber-500 focus:outline-none focus:ring-1 focus:ring-caliber-500/30 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-100"
      >
        <option value="">{allLabel ?? `All ${label.toLowerCase()}`}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <svg
        className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden="true"
      >
        <path d="M6 9l6 6 6-6" />
      </svg>
    </div>
  );
}
