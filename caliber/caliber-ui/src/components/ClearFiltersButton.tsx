interface ClearFiltersButtonProps {
  visible: boolean;
  onClear: () => void;
  className?: string;
  label?: string;
}

export function ClearFiltersButton({
  visible,
  onClear,
  className = "",
  label = "Clear filters",
}: ClearFiltersButtonProps): JSX.Element | null {
  if (!visible) return null;

  return (
    <button
      type="button"
      onClick={onClear}
      className={`inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50 focus:outline-none focus:ring-1 focus:ring-caliber-500/30 dark:border-slate-700/70 dark:bg-slate-950 dark:text-slate-100 dark:hover:border-slate-600 dark:hover:bg-slate-900 ${className}`}
    >
      <svg
        className="h-3.5 w-3.5"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden="true"
      >
        <path d="M18 6 6 18M6 6l12 12" />
      </svg>
      <span>{label}</span>
    </button>
  );
}
