/**
 * Top-level chrome: glass-effect top bar + polished sidebar + content area.
 * Responsive: below ``md:`` breakpoint the sidebar collapses to a drawer.
 */

import { lazy, Suspense, useCallback, useState, type ReactNode } from "react";

import { EdgeToggle } from "./EdgeToggle";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { AssistantPanelProvider, useAssistantPanel } from "./assistant/AssistantPanelContext";

const CaliberAssistantPanel = lazy(
  () => import("./assistant/CaliberAssistantPanel").then((m) => ({ default: m.CaliberAssistantPanel })),
);

interface AppShellProps {
  children: ReactNode;
  currentUser?: string;
  onLogout?: () => void;
}

const COLLAPSE_KEY = "caliber.sidebar.collapsed";

export function AppShell({
  children,
  currentUser,
  onLogout,
}: AppShellProps): JSX.Element {
  return (
    <AssistantPanelProvider>
      <AppShellLayout
        currentUser={currentUser}
        onLogout={onLogout}
      >
        {children}
      </AppShellLayout>
    </AssistantPanelProvider>
  );
}

function AppShellLayout({
  children,
  currentUser,
  onLogout,
}: AppShellProps): JSX.Element {
  const {
    open: assistantOpen,
    effectiveWidth: assistantWidth,
  } = useAssistantPanel();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const closeDrawer = (): void => setDrawerOpen(false);

  // Desktop sidebar collapse (icon rail), persisted across reloads.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(COLLAPSE_KEY) === "true";
  });
  const toggleCollapsed = useCallback((): void => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(COLLAPSE_KEY, String(next));
      } catch {
        // Storage may be disabled — collapsing still works for the session.
      }
      return next;
    });
  }, []);

  return (
    <>
      <TopBar
        onToggleSidebar={() => setDrawerOpen((open) => !open)}
        currentUser={currentUser}
        onLogout={onLogout}
      />
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-16 focus:left-4 focus:bg-white focus:px-3 focus:py-2 focus:z-50 focus:rounded-lg"
      >
        Skip to content
      </a>
      <div className="flex pt-14">
        <Sidebar
          mobileOpen={drawerOpen}
          onNavigate={closeDrawer}
          collapsed={collapsed}
        />
        <EdgeToggle
          dock="left"
          collapsed={collapsed}
          onToggle={toggleCollapsed}
          label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={`hidden md:flex fixed top-[calc(50vh+28px)] z-50 -translate-x-1/2 -translate-y-1/2 transition-[left] duration-200 ${collapsed ? "left-16" : "left-60"}`}
        />
        {drawerOpen && (
          <button
            type="button"
            aria-label="Close navigation"
            className="md:hidden fixed inset-0 top-14 z-30 bg-black/20 backdrop-blur-sm"
            onClick={closeDrawer}
          />
        )}
        <main
          id="main-content"
          data-assistant-open={assistantOpen ? "true" : "false"}
          data-assistant-width={assistantOpen ? String(assistantWidth) : "0"}
          className={`ml-0 ${collapsed ? "md:ml-16" : "md:ml-60"} ${assistantOpen ? "md:mr-[var(--assistant-panel-width)]" : "md:mr-0"} min-w-0 flex-1 min-h-[calc(100vh-56px)] transition-[margin] duration-200`}
        >
          <div className="min-w-0 p-5 sm:p-8">{children}</div>
        </main>
      </div>
      <Suspense fallback={null}>
        <CaliberAssistantPanel />
      </Suspense>
    </>
  );
}
