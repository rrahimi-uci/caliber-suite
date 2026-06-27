/**
 * Fixed top bar — professional glassmorphism header with gradient brand mark,
 * health indicator, the Ask-Aria toggle, the current user, and a dark-mode
 * toggle.
 */

import { useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";

import { AriaLogo } from "@/components/assistant/AriaLogo";
import { useAssistantPanel } from "@/components/assistant/AssistantPanelContext";
import { BrandAcronym, BRAND_ACRONYM_TEXT } from "@/components/BrandAcronym";

import { useTheme } from "./useTheme";

interface TopBarProps {
  onToggleSidebar?: () => void;
  currentUser?: string;
  onLogout?: () => void;
}

/** Polls the CALIBER health endpoint and returns "ok" | "down" | "loading". */
function useHealthStatus(intervalMs = 30_000): "ok" | "down" | "loading" {
  const [status, setStatus] = useState<"ok" | "down" | "loading">("loading");

  useEffect(() => {
    let cancelled = false;
    const check = (): void => {
      caliberApi
        .getHealth()
        .then(() => {
          if (!cancelled) setStatus("ok");
        })
        .catch(() => {
          if (!cancelled) setStatus("down");
        });
    };
    check();
    const id = setInterval(check, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);

  return status;
}

const HEALTH_DOT: Record<string, string> = {
  ok: "bg-emerald-400",
  down: "bg-red-400",
  loading: "bg-slate-400 animate-pulse",
};

export function TopBar({
  onToggleSidebar,
  currentUser,
  onLogout,
}: TopBarProps): JSX.Element {
  const { open: assistantOpen, toggle: toggleAssistant } = useAssistantPanel();
  const { theme, toggle: toggleTheme } = useTheme();
  const staticPrefix =
    (typeof window !== "undefined" && window.__CALIBER_STATIC_PREFIX__) || "";
  const logoSrc = `${staticPrefix}/caliber/caliber-icon.png`;

  const health = useHealthStatus();

  return (
    <header className="fixed left-0 right-0 top-0 z-50 flex h-14 items-center border-b border-slate-200/60 bg-white/80 px-5 shadow-topbar backdrop-blur-xl dark:border-white/10 dark:bg-slate-950/90">
      {/* Hamburger — mobile only */}
      <button
        type="button"
        className="-ml-1 mr-3 rounded-lg p-2 text-slate-500 transition-colors hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-white/10 dark:hover:text-slate-200 md:hidden"
        aria-label="Toggle navigation"
        onClick={onToggleSidebar}
      >
        <svg
          className="w-5 h-5"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          aria-hidden="true"
        >
          <line x1="3" y1="6" x2="21" y2="6" />
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>

      {/* Brand */}
      <div className="flex items-center gap-3">
        <div className="relative">
          <div className="absolute -inset-1 bg-gradient-brand rounded-xl opacity-20 blur-sm" />
          <img
            src={logoSrc}
            alt="CALIBER"
            className="relative w-8 h-8 rounded-lg object-contain"
          />
        </div>
        <div
          className="flex flex-col leading-tight"
          title={`CALIBER : ${BRAND_ACRONYM_TEXT}`}
        >
          <span className="font-bold text-sm tracking-tight bg-gradient-to-r from-caliber-purple to-caliber-500 bg-clip-text text-transparent">
            CALIBER
          </span>
          <BrandAcronym className="hidden lg:block text-[10px] font-normal leading-tight tracking-wide whitespace-nowrap text-slate-500 dark:text-slate-400" />
        </div>
      </div>

      {/* Right actions */}
      <div className="ml-auto flex items-center gap-2">
        {/* Health dot */}
        <span
          className="flex items-center gap-1.5 text-[11px] text-slate-400 mr-1"
          title={`System ${health}`}
        >
          <span className={`w-2 h-2 rounded-full ${HEALTH_DOT[health]}`} />
        </span>

        <button
          type="button"
          aria-label="Ask Aria"
          aria-pressed={assistantOpen}
          onClick={toggleAssistant}
          className={`inline-flex items-center gap-2 rounded-xl border px-3.5 py-2 text-sm font-semibold transition-all duration-200 ${
            assistantOpen
              ? "border-caliber-300 bg-caliber-50 text-caliber-700 dark:border-caliber-500/70 dark:bg-caliber-500/15 dark:text-caliber-200"
              : "border-slate-200/60 bg-slate-50 text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10 dark:hover:text-slate-100"
          }`}
        >
          <AriaLogo className="h-5 w-5" alt="" />
          <span className="hidden sm:inline">Ask Aria</span>
        </button>

        <div className="h-4 w-px bg-slate-200 dark:bg-white/15" />
        {currentUser && (
          <div className="hidden items-center gap-2 rounded-lg border border-slate-200/70 bg-white/70 px-2.5 py-1.5 shadow-sm backdrop-blur-sm dark:border-white/10 dark:bg-white/5 sm:flex">
            <span className="grid h-6 w-6 place-items-center rounded-md bg-slate-900 text-[10px] font-bold uppercase text-white">
              {currentUser.slice(0, 2)}
            </span>
            <span className="max-w-24 truncate text-xs font-semibold text-slate-600 dark:text-slate-200">
              {currentUser}
            </span>
            {onLogout && (
              <button
                type="button"
                className="text-xs font-semibold text-slate-400 transition-colors hover:text-red-600 dark:text-slate-500 dark:hover:text-red-300"
                onClick={onLogout}
              >
                Log out
              </button>
            )}
          </div>
        )}
        <button
          type="button"
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          onClick={toggleTheme}
          className="rounded-lg p-2 text-slate-400 transition-all duration-200 hover:bg-slate-100 hover:text-slate-600 dark:text-slate-500 dark:hover:bg-white/10 dark:hover:text-slate-200"
        >
          {theme === "dark" ? (
            <svg
              className="w-4 h-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
            </svg>
          ) : (
            <svg
              className="w-4 h-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
          )}
        </button>
      </div>
    </header>
  );
}
