/**
 * The single collapse/expand control used by every docked toolbar (main
 * sidebar, Workflow editor palette + inspector). Matches MLflow: a small round
 * chevron button that sits on the panel's edge, vertically centered. The
 * caller positions it via ``className``; this component owns the look + the
 * chevron direction so the affordance is identical everywhere.
 */

interface EdgeToggleProps {
  /** Which side of the screen the panel is docked on. */
  dock: "left" | "right";
  collapsed: boolean;
  onToggle: () => void;
  label: string;
  /** Positioning classes (fixed/absolute, top, left/right, translate, z). */
  className?: string;
}

export function EdgeToggle({
  dock,
  collapsed,
  onToggle,
  label,
  className = "",
}: EdgeToggleProps): JSX.Element {
  // Chevron points the way the panel will move when clicked.
  const pointsLeft = dock === "left" ? !collapsed : collapsed;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={label}
      title={label}
      className={`flex h-6 w-6 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-500 shadow-sm transition-colors hover:bg-slate-50 hover:text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 ${className}`}
    >
      <svg
        className={`h-3.5 w-3.5 transition-transform duration-200 ${pointsLeft ? "" : "rotate-180"}`}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.25"
        aria-hidden="true"
      >
        <path d="M15 18l-6-6 6-6" />
      </svg>
    </button>
  );
}
