/**
 * Cross-UI URL helpers.
 *
 * Goal: keep navigation between CALIBER and MLflow reversible and carry the
 * active light/dark preference when we cross origins (for example direct
 * CALIBER :5001 -> unified gateway :5050).
 */

export type ThemePreference = "light" | "dark";

const CALIBER_THEME_KEY = "caliber.theme";
const MLFLOW_DARK_KEY = "_mlflow_dark_mode_toggle_enabled";
const MLFLOW_PREF_KEY = "databricks-dark-mode-pref";

function normalizeTheme(value: string | null | undefined): ThemePreference | null {
  if (value === "light" || value === "dark") return value;
  if (value === "true") return "dark";
  if (value === "false") return "light";
  return null;
}

function readStoredTheme(): ThemePreference | null {
  if (typeof window === "undefined") return null;
  try {
    const caliber = normalizeTheme(window.localStorage.getItem(CALIBER_THEME_KEY));
    if (caliber) return caliber;
    const mlflow = normalizeTheme(window.localStorage.getItem(MLFLOW_DARK_KEY));
    if (mlflow) return mlflow;
    return normalizeTheme(window.localStorage.getItem(MLFLOW_PREF_KEY));
  } catch {
    return null;
  }
}

function staticPrefix(): string {
  if (typeof window === "undefined") return "";
  return window.__CALIBER_STATIC_PREFIX__ || "";
}

function normalizeHash(hash?: string): string {
  if (!hash) return "";
  if (hash.startsWith("#")) return hash;
  if (hash.startsWith("/#")) return hash.slice(1);
  return `#${hash.replace(/^\//, "")}`;
}

function unifiedOrigin(): string {
  if (typeof window === "undefined") return "";
  const { protocol, hostname, port, origin } = window.location;
  // Direct CALIBER service in the local suite lives on :5001; the gateway
  // (with reversible toolbar + shared MLflow surface) is on :5050.
  if (port === "5001") return `${protocol}//${hostname}:5050`;
  return origin;
}

function withThemeHint(
  path: string,
  theme: ThemePreference | null,
  seed?: URLSearchParams,
): string {
  const params = seed ? new URLSearchParams(seed) : new URLSearchParams();
  if (theme) params.set("theme", theme);
  return params.size ? `${path}?${params.toString()}` : path;
}

export function appendThemeHintToUrl(
  href: string,
  theme: ThemePreference | null = readStoredTheme(),
): string {
  if (!theme || !href) return href;
  try {
    if (typeof window === "undefined") return href;
    const url = new URL(href, window.location.origin);
    url.searchParams.set("theme", theme);
    const absolute = /^[a-z][a-z0-9+.-]*:\/\//i.test(href);
    return absolute
      ? url.toString()
      : `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return href;
  }
}

export function buildCaliberHref(options?: {
  theme?: ThemePreference | null;
}): string {
  const theme = options?.theme ?? readStoredTheme();
  const prefix = staticPrefix();
  const relative = withThemeHint(`${prefix}/caliber/`, theme);
  if (typeof window === "undefined" || prefix || window.location.port !== "5001") {
    return relative;
  }
  return `${unifiedOrigin()}${relative}`;
}

export function buildMlflowHref(options?: {
  hash?: string;
  theme?: ThemePreference | null;
}): string {
  const theme = options?.theme ?? readStoredTheme();
  const hash = normalizeHash(options?.hash);
  const prefix = staticPrefix();

  if (prefix) {
    return `${withThemeHint(`${prefix}/`, theme)}${hash}`;
  }

  const relative = `${withThemeHint("/", theme, new URLSearchParams({ ui: "mlflow" }))}${hash}`;
  if (typeof window === "undefined" || window.location.port !== "5001") {
    return relative;
  }
  return `${unifiedOrigin()}${relative}`;
}
