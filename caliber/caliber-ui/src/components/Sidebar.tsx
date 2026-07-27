/**
 * Left navigation — polished sidebar with gradient branding area,
 * grouped sections, and animated active states.
 */

import { createContext, useContext, type ReactNode } from "react";
import {
  Activity,
  BookOpen,
  Database,
  FileText,
  FlaskConical,
  Gauge,
  ClipboardCheck,
  LayoutDashboard,
  ListChecks,
  Network,
  MessageSquareText,
  Scale,
  PlugZap,
  Puzzle,
  Rocket,
  ScrollText,
  Settings2,
  Workflow,
  Wrench,
} from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import {
  HEALTH_DOT,
  HEALTH_TITLE,
  HEALTH_LABEL,
  type HealthStatus,
} from "@/components/useHealthStatus";
import { useApiQuery } from "@/hooks/useApiQuery";

interface Badge {
  count: number;
  tone: "red" | "amber" | "blue";
  label: string;
}

interface NavItemProps {
  to: string;
  icon: ReactNode;
  label: string;
  badge?: Badge;
  small?: boolean;
  external?: boolean;
  onNavigate?: () => void;
}

const TONE_CLASS: Record<Badge["tone"], string> = {
  red: "bg-red-50 text-red-600 ring-1 ring-red-200/50",
  amber: "bg-amber-50 text-amber-600 ring-1 ring-amber-200/50",
  blue: "bg-blue-50 text-blue-600 ring-1 ring-blue-200/50",
};

// Solid dots shown on the icon when collapsed (the numeric badge is hidden).
const DOT_CLASS: Record<Badge["tone"], string> = {
  red: "bg-red-500",
  amber: "bg-amber-500",
  blue: "bg-blue-500",
};

const NAV_ICON_CLASS = "h-[18px] w-[18px]";

// Whether the sidebar is collapsed to an icon rail. Read by NavItem/Section so
// the collapsed styling doesn't have to be threaded through every call site.
// Collapsing is a desktop affordance — all collapsed styles are ``md:``-scoped
// so the mobile drawer always shows full labels.
const CollapsedContext = createContext(false);

function NavItem({
  to,
  icon,
  label,
  badge,
  small = false,
  external = false,
  onNavigate,
}: NavItemProps): JSX.Element {
  const location = useLocation();
  const collapsed = useContext(CollapsedContext);
  const isActive =
    !external && (location.pathname === to || location.pathname.startsWith(`${to}/`));
  const baseSize = small ? "text-xs" : "text-[13px]";
  const className = `nav-item ${baseSize} ${isActive ? "nav-item-active" : ""} ${collapsed ? "md:justify-center md:px-0" : ""}`;

  const content = (
    <>
      <span className={`relative ${small ? "w-4 h-4" : "w-[18px] h-[18px]"} flex-shrink-0 ${isActive ? "text-caliber-purple" : "text-slate-400"}`}>
        {icon}
        {badge && badge.count > 0 && (
          <span
            className={`absolute -top-1 -right-1 w-2 h-2 rounded-full ${DOT_CLASS[badge.tone]} ${collapsed ? "hidden md:block" : "hidden"}`}
            aria-hidden="true"
          />
        )}
      </span>
      <span className={`flex-1 font-medium ${collapsed ? "md:hidden" : ""}`}>{label}</span>
      {badge && badge.count > 0 && (
        <span
          className={`ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded-md ${TONE_CLASS[badge.tone]} ${collapsed ? "md:hidden" : ""}`}
          aria-label={badge.label}
        >
          {badge.count}
        </span>
      )}
    </>
  );

  // External targets (e.g. the standalone docs site) open in a new tab via a
  // plain anchor — they aren't React routes.
  if (external) {
    return (
      <a
        href={to}
        target="_blank"
        rel="noopener noreferrer"
        className={className}
        onClick={onNavigate}
        title={collapsed ? label : undefined}
      >
        {content}
      </a>
    );
  }

  return (
    <NavLink
      to={to}
      className={className}
      end={false}
      onClick={onNavigate}
      title={collapsed ? label : undefined}
    >
      {content}
    </NavLink>
  );
}

interface SidebarProps {
  health: HealthStatus;
  mobileOpen?: boolean;
  onNavigate?: () => void;
  /** Desktop: collapse the sidebar to an icon rail (the toggle lives in AppShell). */
  collapsed?: boolean;
}

export function Sidebar({
  health,
  mobileOpen = false,
  onNavigate,
  collapsed = false,
}: SidebarProps): JSX.Element {
  const translate = mobileOpen ? "translate-x-0" : "-translate-x-full";
  // Collapse only applies on desktop (``md:``); the mobile drawer is full width.
  const width = collapsed ? "w-60 md:w-16" : "w-60";

  // "Needs you" — plans paused awaiting a human decision (gate / approval /
  // below-gate confirm). Surfaced on the Plans nav so an in-flight plan that
  // stopped for you isn't lost when you navigate away from chat. Resilient: any
  // fetch failure simply shows no badge.
  const plansNav = useApiQuery(
    ["aria", "plans", "needs-you"],
    (signal) => caliberApi.listAriaPlans(null, signal),
    { refetchInterval: 30_000, staleTime: 15_000, retry: false },
  );
  const pausedCount = (plansNav.data ?? []).filter((p) => p.status === "paused").length;
  return (
    <CollapsedContext.Provider value={collapsed}>
    <aside
      className={`${width} h-[calc(100vh-56px)] bg-gradient-sidebar border-r border-slate-200/60 fixed left-0 top-14 overflow-x-hidden overflow-y-auto z-40 transition-[transform,width] duration-200 md:translate-x-0 ${translate}`}
      aria-label="CALIBER navigation"
    >
      <nav className="py-4 space-y-1 flex flex-col min-h-full">
        <NavItem
          to="/"
          icon={<LayoutDashboard className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Dashboard"
          onNavigate={onNavigate}
        />

        {/* Compose — author & run agentic workflows; Aria plans orchestrate them. */}
        <Section title="Compose" />
        <NavItem
          to="/workflows"
          icon={<Workflow className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Workflows"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/aria/plans"
          icon={<ListChecks className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Plans"
          badge={
            pausedCount > 0
              ? {
                  count: pausedCount,
                  tone: "amber",
                  label: `${pausedCount} plan${pausedCount === 1 ? "" : "s"} awaiting your input`,
                }
              : undefined
          }
          onNavigate={onNavigate}
        />

        {/* Library — reusable, governed components the Compose surfaces reference. */}
        <Section title="Library" />
        <NavItem
          to="/prompts"
          icon={<MessageSquareText className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Prompts"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/tools"
          icon={<Wrench className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Tools"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/skills"
          icon={<Puzzle className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Skills"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/mcp-servers"
          icon={<PlugZap className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="MCP Servers"
          onNavigate={onNavigate}
        />

        {/* Knowledge — grounding sources agents retrieve over. */}
        <Section title="Knowledge" />
        <NavItem
          to="/knowledge-bases"
          icon={<BookOpen className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Knowledge Base"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/object-store"
          icon={<Database className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Object Store"
          onNavigate={onNavigate}
        />

        {/* Evaluate — datasets, judges, and scored runs. */}
        <Section title="Evaluate" />
        <NavItem
          to="/eval-datasets"
          icon={<FlaskConical className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Test Sets"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/judges"
          icon={<Scale className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Judges"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/evaluations"
          icon={<Gauge className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Evaluations"
          onNavigate={onNavigate}
        />

        {/* Observe — runtime traces and structured human review. */}
        <Section title="Observe" />
        <NavItem
          to="/observability"
          icon={<Activity className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Observability"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/review-queues"
          icon={<ClipboardCheck className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Review Queues"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/releases"
          icon={<Rocket className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Releases"
          onNavigate={onNavigate}
        />
        <NavItem
          to="/audit-log"
          icon={<ScrollText className={NAV_ICON_CLASS} strokeWidth={1.85} />}
          label="Audit Log"
          onNavigate={onNavigate}
        />

        {/* Platform links pinned to the bottom of the sidebar. */}
        <div className="mt-auto pt-2">
          <Section title="Platform" />
          <NavItem
            to="/gateway"
            icon={<Network className={NAV_ICON_CLASS} strokeWidth={1.85} />}
            label="LLM Gateway"
            onNavigate={onNavigate}
          />
          <NavItem
            to="/settings"
            icon={<Settings2 className={NAV_ICON_CLASS} strokeWidth={1.85} />}
            label="Settings"
            onNavigate={onNavigate}
          />
          <NavItem
            to={`${import.meta.env.BASE_URL}docs/index.html`}
            icon={<FileText className={NAV_ICON_CLASS} strokeWidth={1.85} />}
            label="Docs"
            external
            onNavigate={onNavigate}
          />

          {/* Footer status (hidden in the collapsed rail). Derived from the
              real /health poll — a decorative always-green dot here taught
              users to ignore the status lights. */}
          <div className={`mt-4 mx-4 pt-4 border-t border-slate-200/60 ${collapsed ? "md:hidden" : ""}`}>
            <div
              className="flex items-center gap-2 px-2"
              data-testid="sidebar-health"
              title={HEALTH_TITLE[health]}
            >
              <div
                className={`w-2 h-2 rounded-full ${HEALTH_DOT[health]}`}
                aria-hidden="true"
              />
              <span className="text-[10px] font-medium text-slate-400">
                {HEALTH_LABEL[health]}
              </span>
            </div>
          </div>
        </div>
      </nav>
    </aside>
    </CollapsedContext.Provider>
  );
}

function Section({ title }: { title: string }): JSX.Element {
  const collapsed = useContext(CollapsedContext);
  return (
    <div className="px-4 mb-1 mt-6 first:mt-0">
      <span className={`text-[10px] font-bold uppercase tracking-widest text-slate-300 px-2 ${collapsed ? "md:hidden" : ""}`}>
        {title}
      </span>
    </div>
  );
}
