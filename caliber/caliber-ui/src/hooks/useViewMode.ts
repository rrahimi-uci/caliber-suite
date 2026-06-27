/**
 * useViewMode — remembers whether a card-based list page is showing its
 * card **grid** or a compact **list**, persisted per page under
 * ``caliber.viewmode.<key>`` in ``localStorage``.
 *
 * The default is ``"grid"`` everywhere so the existing card layout stays the
 * landing experience; the toggle only opts a user into the denser list view.
 * Storage access is guarded (try/catch) so it is safe under SSR / jsdom and
 * when a browser has storage disabled — the choice simply falls back to the
 * in-memory default for that session.
 */

import { useCallback, useState } from "react";

export type ViewMode = "grid" | "list";

const STORAGE_PREFIX = "caliber.viewmode.";

function storageKey(key: string): string {
  return `${STORAGE_PREFIX}${key}`;
}

function readInitial(key: string): ViewMode {
  if (typeof window === "undefined") return "grid";
  try {
    const stored = window.localStorage.getItem(storageKey(key));
    if (stored === "grid" || stored === "list") return stored;
  } catch {
    // Storage may be unavailable (private mode / disabled) — use the default.
  }
  return "grid";
}

export function useViewMode(key: string): [ViewMode, (value: ViewMode) => void] {
  const [mode, setMode] = useState<ViewMode>(() => readInitial(key));

  const update = useCallback(
    (value: ViewMode) => {
      setMode(value);
      if (typeof window === "undefined") return;
      try {
        window.localStorage.setItem(storageKey(key), value);
      } catch {
        // Persisting is best-effort; the toggle still works for the session.
      }
    },
    [key],
  );

  return [mode, update];
}
