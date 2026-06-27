/**
 * Global open/close state for the Caliber Assistant drawer panel.
 *
 * Wraps the entire app so the sidebar toggle and panel itself can
 * share state without prop drilling.
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export const ASSISTANT_PANEL_DEFAULT_WIDTH = 380;
export const ASSISTANT_PANEL_MIN_WIDTH = 320;
export const ASSISTANT_PANEL_MAX_WIDTH = 760;
export const ASSISTANT_PANEL_COLLAPSED_WIDTH = 64;

const PANEL_WIDTH_KEY = "caliber.assistant.panel.width";
const PANEL_OPEN_KEY = "caliber.assistant.panel.open";
const PANEL_COLLAPSED_KEY = "caliber.assistant.panel.collapsed";

function clampPanelWidth(width: number): number {
  if (Number.isNaN(width)) return ASSISTANT_PANEL_DEFAULT_WIDTH;
  return Math.min(ASSISTANT_PANEL_MAX_WIDTH, Math.max(ASSISTANT_PANEL_MIN_WIDTH, width));
}

function readPersistedBool(key: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function writePersistedBool(key: string, value: boolean): void {
  try {
    window.localStorage.setItem(key, String(value));
  } catch {
    // Ignore storage failures; state still applies for the current session.
  }
}

interface AssistantPanelState {
  open: boolean;
  collapsed: boolean;
  panelWidth: number;
  effectiveWidth: number;
  toggle: () => void;
  close: () => void;
  setPanelWidth: (width: number) => void;
  collapse: () => void;
  expand: () => void;
  toggleCollapsed: () => void;
}

const AssistantPanelContext = createContext<AssistantPanelState>({
  open: false,
  collapsed: false,
  panelWidth: ASSISTANT_PANEL_DEFAULT_WIDTH,
  effectiveWidth: ASSISTANT_PANEL_DEFAULT_WIDTH,
  toggle: () => {},
  close: () => {},
  setPanelWidth: () => {},
  collapse: () => {},
  expand: () => {},
  toggleCollapsed: () => {},
});

export function AssistantPanelProvider({ children }: { children: ReactNode }): JSX.Element {
  // Persist open/collapsed so a refresh keeps the assistant where the user left
  // it — an open panel stays open (with its session restored by the panel).
  const [open, setOpen] = useState<boolean>(() => readPersistedBool(PANEL_OPEN_KEY));
  const [collapsed, setCollapsed] = useState<boolean>(() =>
    readPersistedBool(PANEL_COLLAPSED_KEY),
  );
  const [panelWidth, setPanelWidthState] = useState<number>(() => {
    if (typeof window === "undefined") return ASSISTANT_PANEL_DEFAULT_WIDTH;
    const raw = window.localStorage.getItem(PANEL_WIDTH_KEY);
    if (raw === null) return ASSISTANT_PANEL_DEFAULT_WIDTH;
    const parsed = Number(raw);
    return Number.isFinite(parsed)
      ? clampPanelWidth(parsed)
      : ASSISTANT_PANEL_DEFAULT_WIDTH;
  });

  const setPanelWidth = useCallback((width: number) => {
    const next = clampPanelWidth(width);
    setPanelWidthState(next);
    try {
      window.localStorage.setItem(PANEL_WIDTH_KEY, String(next));
    } catch {
      // Ignore storage failures; width still applies for current session.
    }
  }, []);

  const toggle = useCallback(() => {
    setOpen((v) => {
      const next = !v;
      if (next) {
        setCollapsed(false);
      }
      return next;
    });
  }, []);
  const close = useCallback(() => {
    setOpen(false);
    setCollapsed(false);
  }, []);
  const collapse = useCallback(() => setCollapsed(true), []);
  const expand = useCallback(() => setCollapsed(false), []);
  const toggleCollapsed = useCallback(() => setCollapsed((v) => !v), []);
  const effectiveWidth = collapsed ? ASSISTANT_PANEL_COLLAPSED_WIDTH : panelWidth;

  useEffect(() => {
    writePersistedBool(PANEL_OPEN_KEY, open);
  }, [open]);

  useEffect(() => {
    writePersistedBool(PANEL_COLLAPSED_KEY, collapsed);
  }, [collapsed]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.style.setProperty(
      "--assistant-panel-width",
      open ? `${effectiveWidth}px` : "0px",
    );
  }, [open, effectiveWidth]);

  return (
    <AssistantPanelContext.Provider
      value={{
        open,
        collapsed,
        panelWidth,
        effectiveWidth,
        toggle,
        close,
        setPanelWidth,
        collapse,
        expand,
        toggleCollapsed,
      }}
    >
      {children}
    </AssistantPanelContext.Provider>
  );
}

export function useAssistantPanel(): AssistantPanelState {
  return useContext(AssistantPanelContext);
}
