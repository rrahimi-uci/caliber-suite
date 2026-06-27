import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

type AssistantControlTone = "default" | "warning";

interface AssistantControlDropdownProps {
  align?: "left" | "right";
  ariaLabel: string;
  children: (helpers: { closeMenu: () => void }) => ReactNode;
  className?: string;
  disabled?: boolean;
  icon?: ReactNode;
  menuClassName?: string;
  testId?: string;
  title?: string;
  tone?: AssistantControlTone;
  value: string;
  valueClassName?: string;
}

interface AssistantControlDropdownOptionProps {
  description?: string;
  disabled?: boolean;
  label: string;
  onClick: () => void;
  secondaryLabel?: string;
  selected?: boolean;
  testId?: string;
}

export function AssistantControlDropdown({
  align = "left",
  ariaLabel,
  children,
  className,
  disabled,
  icon,
  menuClassName,
  testId,
  title,
  tone = "default",
  value,
  valueClassName,
}: AssistantControlDropdownProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;

    const handleMouseDown = (event: MouseEvent): void => {
      if (rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };

    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setOpen(false);
    };

    window.addEventListener("mousedown", handleMouseDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const closeMenu = (): void => setOpen(false);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={ariaLabel}
        data-testid={testId}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        title={title}
        className={cn(
          "inline-flex h-9 max-w-full items-center gap-2 rounded-full border px-3 text-[12px] font-medium shadow-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-caliber-400/35 disabled:cursor-not-allowed disabled:opacity-50",
          tone === "warning"
            ? "border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-400/30 dark:bg-amber-500/12 dark:text-amber-200 dark:hover:bg-amber-500/18"
            : "border-slate-200 bg-slate-100 text-slate-700 hover:bg-slate-200/80 dark:border-slate-700 dark:bg-slate-800/90 dark:text-slate-100 dark:hover:bg-slate-800",
          className,
        )}
      >
        {icon ? (
          <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center">
            {icon}
          </span>
        ) : null}
        <span className={cn("truncate", valueClassName)}>{value}</span>
        <svg
          className={cn("h-3.5 w-3.5 shrink-0 transition-transform", open && "rotate-180")}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          aria-hidden="true"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open && (
        <div
          id={menuId}
          role="menu"
          className={cn(
            "absolute bottom-full z-40 mb-2 min-w-[240px] overflow-hidden rounded-[22px] border border-slate-200/90 bg-white/95 p-2 shadow-2xl backdrop-blur dark:border-slate-700/90 dark:bg-[#262a33]/95",
            align === "left" ? "left-0" : "right-0",
            menuClassName,
          )}
        >
          {children({ closeMenu })}
        </div>
      )}
    </div>
  );
}

export function AssistantControlDropdownSection({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  return (
    <p className="px-3 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-400 dark:text-slate-500">
      {children}
    </p>
  );
}

export function AssistantControlDropdownOption({
  description,
  disabled,
  label,
  onClick,
  secondaryLabel,
  selected,
  testId,
}: AssistantControlDropdownOptionProps): JSX.Element {
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={selected}
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex w-full items-start gap-3 rounded-[16px] px-3 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-50",
        selected
          ? "bg-slate-100 text-slate-950 dark:bg-slate-800 dark:text-slate-50"
          : "text-slate-700 hover:bg-slate-100/80 dark:text-slate-200 dark:hover:bg-slate-800/70",
      )}
    >
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="truncate text-[13px] font-medium">{label}</span>
          {secondaryLabel ? (
            <span className="rounded-full bg-slate-200/80 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500 dark:bg-slate-700/80 dark:text-slate-300">
              {secondaryLabel}
            </span>
          ) : null}
        </span>
        {description ? (
          <span className="mt-0.5 block text-[12px] leading-5 text-slate-500 dark:text-slate-400">
            {description}
          </span>
        ) : null}
      </span>
      <span
        className={cn(
          "mt-1 inline-flex h-4 w-4 shrink-0 items-center justify-center text-caliber-600 dark:text-caliber-300",
          !selected && "opacity-0",
        )}
        aria-hidden="true"
      >
        <svg viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
          <path
            fillRule="evenodd"
            d="M16.704 5.29a1 1 0 010 1.414l-7.01 7.01a1 1 0 01-1.414 0L4.79 10.224a1 1 0 011.414-1.414l2.783 2.782 6.303-6.303a1 1 0 011.414 0z"
            clipRule="evenodd"
          />
        </svg>
      </span>
    </button>
  );
}
