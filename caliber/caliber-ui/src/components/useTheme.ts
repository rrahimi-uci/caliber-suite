/**
 * Light/dark theme toggle, persisted to localStorage.
 *
 * The theme is applied by adding/removing a ``dark`` class on the
 * ``<html>`` element. Tailwind's ``darkMode: "class"`` strategy plus the
 * dark overrides in ``styles/index.css`` do the rest.
 *
 * On first load we read the persisted preference, falling back to the
 * OS-level ``prefers-color-scheme`` so users who set their system to
 * dark mode get the dark UI without having to flip a switch.
 *
 * MLflow alignment (best-effort): MLflow's own dark mode initialises from
 * ``_mlflow_dark_mode_toggle_enabled`` ("true"/"false") and writes
 * ``databricks-dark-mode-pref`` ("light"/"dark"). We mirror CALIBER's choice
 * into both keys and consume them as fallbacks. This only carries across when
 * the two UIs happen to share an origin (e.g. behind a reverse proxy); CALIBER
 * and MLflow are served on separate ports by default, so it's harmless either
 * way.
 */

import { useEffect, useState, useCallback } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "caliber.theme";
// The localStorage key MLflow 3.x reads on load to decide its dark mode.
const MLFLOW_DARK_KEY = "_mlflow_dark_mode_toggle_enabled";
const MLFLOW_PREF_KEY = "databricks-dark-mode-pref";
const THEME_EVENT = "caliber-theme-changed";
const THEME_QUERY_KEY = "theme";

function normalizeTheme(value: string | null): Theme | null {
  if (value === "light" || value === "dark") return value;
  if (value === "true") return "dark";
  if (value === "false") return "light";
  return null;
}

function readInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const queryTheme = normalizeTheme(
    new URLSearchParams(window.location.search).get(THEME_QUERY_KEY),
  );
  if (queryTheme) return queryTheme;
  // Honour a choice the user may have made on the MLflow side first.
  const mlflow = normalizeTheme(window.localStorage.getItem(MLFLOW_DARK_KEY));
  if (mlflow) return mlflow;
  const mlflowPref = normalizeTheme(window.localStorage.getItem(MLFLOW_PREF_KEY));
  if (mlflowPref) return mlflowPref;
  const persisted = normalizeTheme(window.localStorage.getItem(STORAGE_KEY));
  if (persisted) return persisted;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  const html = document.documentElement;
  html.classList.toggle("dark", theme === "dark");
  html.style.colorScheme = theme;
}

function syncMlflowTheme(theme: Theme): void {
  if (typeof window === "undefined") return;
  try {
    // MLflow's native dark-mode preference (read same-origin behind the gateway).
    window.localStorage.setItem(MLFLOW_DARK_KEY, theme === "dark" ? "true" : "false");
    // Companion key MLflow also writes.
    window.localStorage.setItem(MLFLOW_PREF_KEY, theme);
  } catch {
    // Storage may be disabled — CALIBER's own theme still works.
  }
}

function notifyThemeChange(theme: Theme): void {
  if (typeof window === "undefined") return;
  try {
    window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { theme } }));
  } catch {
    // Ignore event dispatch failures (older browsers / constrained envs).
  }
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(readInitialTheme);

  useEffect(() => {
    applyTheme(theme);
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Storage may be disabled — toggling still works for the session.
    }
    syncMlflowTheme(theme);
    notifyThemeChange(theme);
  }, [theme]);

  // Pick up a theme change made in another tab / the MLflow UI (same origin).
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onStorage = (event: StorageEvent): void => {
      if (event.key === STORAGE_KEY && (event.newValue === "light" || event.newValue === "dark")) {
        setTheme(event.newValue);
      } else if (event.key === MLFLOW_DARK_KEY && event.newValue !== null) {
        setTheme(event.newValue === "true" ? "dark" : "light");
      } else if (
        event.key === MLFLOW_PREF_KEY &&
        (event.newValue === "light" || event.newValue === "dark")
      ) {
        setTheme(event.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const toggle = useCallback(() => {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }, []);

  return { theme, toggle };
}
