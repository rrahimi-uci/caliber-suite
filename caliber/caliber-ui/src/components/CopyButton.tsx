/**
 * CopyButton — a tiny inline "copy this value to the clipboard" affordance.
 *
 * IDs (tool/skill/entity ids), models, and endpoint URLs are shown all over the
 * app in `font-mono` spans that users routinely need to paste elsewhere. Before
 * this they had to hand-select the text; this is the shared one-click control.
 *
 * It degrades gracefully when the Clipboard API is unavailable (older browsers,
 * non-secure contexts) by surfacing an error toast rather than throwing, and it
 * flashes a check for ~1.2s so the click feels acknowledged.
 */

import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { showToast } from "../lib/toast";

export interface CopyButtonProps {
  /** The exact text written to the clipboard. */
  value: string;
  /** Accessible label / tooltip; defaults to "Copy". */
  label?: string;
  /** Toast shown on success; defaults to "Copied to clipboard." */
  successMessage?: string;
  className?: string;
  testId?: string;
}

export function CopyButton({
  value,
  label = "Copy",
  successMessage = "Copied to clipboard.",
  className = "",
  testId,
}: CopyButtonProps): JSX.Element {
  const [copied, setCopied] = useState(false);
  // Clear the transient "copied" flash on unmount so we never setState on a
  // gone component (e.g. the row was filtered away right after a click).
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const onClick = (event: React.MouseEvent): void => {
    // These buttons usually sit inside a clickable row/card — don't let the
    // copy bubble up and also "open" the item.
    event.stopPropagation();
    if (!navigator.clipboard?.writeText) {
      showToast.error("Clipboard is unavailable in this browser.");
      return;
    }
    void navigator.clipboard.writeText(value).then(
      () => {
        setCopied(true);
        showToast.success(successMessage);
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1200);
      },
      () => showToast.error("Failed to copy to clipboard."),
    );
  };

  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      data-testid={testId}
      className={`inline-flex items-center justify-center rounded text-slate-400 transition-colors hover:text-slate-700 focus:outline-none focus:ring-1 focus:ring-caliber-500/30 dark:hover:text-slate-200 ${className}`}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-emerald-500" aria-hidden="true" />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden="true" />
      )}
    </button>
  );
}
