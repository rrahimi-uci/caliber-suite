/**
 * Left navigation — polished sidebar with gradient branding area,
 * collapsible grouped sections, and animated active states.
 *
 * The information architecture lives in ``NAV_GROUPS`` rather than in JSX so
 * the grouping is one readable list instead of twenty-two hand-placed blocks,
 * and so tests can assert the structure directly.
 *
 * Two independent "collapsed" concepts meet here and must not be confused:
 *
 *   * the desktop **icon rail** (``collapsed`` prop) hides every label and
 *     renders the destinations as a flat strip of icons — per-group open/closed
 *     state is meaningless there, so the rail always shows every item;
 *   * a **closed group** hides its items in the full-width sidebar. That is the
 *     density control, and it is the only thing that reduces what the first
 *     viewport shows: regrouping alone kept the section count at six.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Activity,
  Bot,
  BookOpen,
  ChevronRight,
  Database,
  FileText,
  FlaskConical,
  Gauge,
  ClipboardCheck,
  Globe,
  LayoutDashboard,
  Library,
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
  ShieldCheck,
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

/** localStorage key holding the ids of the groups the user left open. */
const OPEN_GROUPS_KEY = "caliber.nav.open-groups";

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

interface NavEntry {
  to: string;
  icon: ReactNode;
  label: string;
  external?: boolean;
  /** Key resolved against the live badge map at render time. */
  badgeKey?: "plans";
}

interface NavGroup {
  id: string;
  title: string;
  items: NavEntry[];
}

/**
 * The navigation information architecture.
 *
 * ``Integrations`` is the substantive regrouping: MCP Servers used to sit in
 * *Library* beside authored assets (prompts, tools, skills) while the LLM
 * Gateway sat in *Platform* beside Settings and Docs. Both are connections to
 * external systems, and splitting them across those two groups meant neither
 * read as an integration.
 */
const NAV_GROUPS: NavGroup[] = [
  {
    id: "build",
    title: "Build",
    items: [
      { to: "/workflows", icon: <Workflow className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Workflows" },
      { to: "/cookbooks", icon: <BookOpen className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Cookbooks" },
      { to: "/agents", icon: <Bot className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Agents" },
      { to: "/aria/plans", icon: <ListChecks className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Plans", badgeKey: "plans" },
    ],
  },
  {
    id: "resources",
    title: "Resources",
    items: [
      { to: "/prompts", icon: <MessageSquareText className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Prompts" },
      { to: "/skills", icon: <Puzzle className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Skills" },
      { to: "/tools", icon: <Wrench className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Tools" },
      { to: "/knowledge-bases", icon: <Library className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Knowledge Bases" },
      // Deliberately still "Object Store", not "Files": ``routes/files.py`` is a
      // different concept (workflow file staging and run-scoped uploads), and
      // the docs series is docs/07-object-store/. Renaming only the nav label
      // would collide with one and diverge from the other.
      { to: "/object-store", icon: <Database className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Object Store" },
    ],
  },
  {
    id: "integrations",
    title: "Integrations",
    items: [
      { to: "/mcp-servers", icon: <PlugZap className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "MCP Servers" },
      { to: "/openapi-integrations", icon: <Globe className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "OpenAPI Integrations" },
      { to: "/gateway", icon: <Network className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "LLM Gateway" },
    ],
  },
  {
    id: "evaluate",
    title: "Evaluate",
    items: [
      { to: "/eval-datasets", icon: <FlaskConical className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Test Sets" },
      { to: "/judges", icon: <Scale className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Judges" },
      { to: "/evaluations", icon: <Gauge className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Evaluations" },
    ],
  },
  {
    id: "operate",
    title: "Operate",
    items: [
      { to: "/observability", icon: <Activity className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Observability" },
      { to: "/review-queues", icon: <ClipboardCheck className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Review Queues" },
      { to: "/releases", icon: <Rocket className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Releases" },
      { to: "/audit-log", icon: <ScrollText className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Audit Log" },
    ],
  },
  {
    id: "admin",
    title: "Admin",
    items: [
      { to: "/administration", icon: <ShieldCheck className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Administration" },
      { to: "/settings", icon: <Settings2 className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Settings" },
      { to: `${import.meta.env.BASE_URL}docs/index.html`, icon: <FileText className={NAV_ICON_CLASS} strokeWidth={1.85} />, label: "Docs", external: true },
    ],
  },
];

/** Whether ``pathname`` is inside this destination (same rule as NavItem). */
function matchesPath(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`);
}

function activeGroupId(pathname: string): string | null {
  const group = NAV_GROUPS.find((candidate) =>
    candidate.items.some((item) => !item.external && matchesPath(pathname, item.to)),
  );
  return group ? group.id : null;
}

function readOpenGroups(): string[] | null {
  try {
    const raw = window.localStorage.getItem(OPEN_GROUPS_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    return parsed.filter((value): value is string => typeof value === "string");
  } catch {
    // Private mode, a quota error, or hand-corrupted JSON. Navigation must not
    // depend on storage succeeding.
    return null;
  }
}

function writeOpenGroups(ids: string[]): void {
  try {
    window.localStorage.setItem(OPEN_GROUPS_KEY, JSON.stringify(ids));
  } catch {
    /* non-fatal — the session keeps its in-memory state */
  }
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
  const location = useLocation();
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

  const badges: Record<string, Badge | undefined> = useMemo(
    () => ({
      plans:
        pausedCount > 0
          ? {
              count: pausedCount,
              tone: "amber",
              label: `${pausedCount} plan${pausedCount === 1 ? "" : "s"} awaiting your input`,
            }
          : undefined,
    }),
    [pausedCount],
  );

  const activeGroup = activeGroupId(location.pathname);

  // Default to the active group alone: the point of the change is that the
  // first viewport stops showing all twenty-two destinations at once.
  //
  // Falling back to "build" matters. On the Dashboard no group is active, and
  // an empty default would open the app on six collapsed headers and zero
  // destinations — denser than asked for, and a worse first run than the
  // problem being solved. Build is where the primary work starts.
  const [openGroups, setOpenGroups] = useState<string[]>(
    () => readOpenGroups() ?? [activeGroup ?? "build"],
  );

  // Auto-open the group for the route the user lands on (a direct link to
  // /mcp-servers opens Integrations). Keyed on *changes* to the active group so
  // that manually closing the group you are already in is not undone on the
  // next render.
  const lastActiveGroup = useRef<string | null>(activeGroup);
  useEffect(() => {
    if (activeGroup === lastActiveGroup.current) return;
    lastActiveGroup.current = activeGroup;
    if (!activeGroup) return;
    setOpenGroups((current) =>
      current.includes(activeGroup) ? current : [...current, activeGroup],
    );
  }, [activeGroup]);

  useEffect(() => {
    writeOpenGroups(openGroups);
  }, [openGroups]);

  const toggleGroup = useCallback((id: string) => {
    setOpenGroups((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  }, []);

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

        {NAV_GROUPS.map((group) => (
          <NavGroupSection
            key={group.id}
            group={group}
            badges={badges}
            open={collapsed || openGroups.includes(group.id)}
            onToggle={() => toggleGroup(group.id)}
            onNavigate={onNavigate}
            // Admin is pinned to the bottom, as the Platform group used to be.
            pinToBottom={group.id === "admin"}
          />
        ))}

        {/* Footer status (hidden in the collapsed rail). Derived from the
            real /health poll — a decorative always-green dot here taught
            users to ignore the status lights. Deliberately left visible
            rather than folded into a menu. */}
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
      </nav>
    </aside>
    </CollapsedContext.Provider>
  );
}

interface NavGroupSectionProps {
  group: NavGroup;
  badges: Record<string, Badge | undefined>;
  open: boolean;
  onToggle: () => void;
  onNavigate?: () => void;
  pinToBottom?: boolean;
}

function NavGroupSection({
  group,
  badges,
  open,
  onToggle,
  onNavigate,
  pinToBottom = false,
}: NavGroupSectionProps): JSX.Element {
  const collapsed = useContext(CollapsedContext);
  const location = useLocation();
  const panelId = `nav-group-${group.id}`;
  const hasActiveItem = group.items.some(
    (item) => !item.external && matchesPath(location.pathname, item.to),
  );

  // Roll hidden badges up to the header. Without this, closing a group would
  // silently suppress the "needs you" count the badge exists to surface —
  // trading an operator-facing signal for whitespace.
  const rolledUp = open
    ? undefined
    : group.items
        .map((item) => (item.badgeKey ? badges[item.badgeKey] : undefined))
        .find((badge) => badge && badge.count > 0);

  return (
    <div className={pinToBottom ? "mt-auto pt-2" : undefined}>
      <div className={`px-4 mb-1 mt-6 first:mt-0 ${collapsed ? "md:hidden" : ""}`}>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={panelId}
          data-testid={`nav-group-toggle-${group.id}`}
          className="flex w-full items-center gap-1.5 px-2 py-1 rounded-md text-[10px] font-bold uppercase tracking-widest text-slate-400 hover:text-slate-600 hover:bg-slate-100/60 transition-colors"
        >
          <ChevronRight
            className={`h-3 w-3 flex-shrink-0 transition-transform duration-150 ${open ? "rotate-90" : ""}`}
            aria-hidden="true"
          />
          <span className="flex-1 text-left">{group.title}</span>
          {hasActiveItem && !open && (
            <span
              className="h-1.5 w-1.5 rounded-full bg-caliber-purple"
              aria-hidden="true"
            />
          )}
          {rolledUp && (
            <span
              className={`text-[10px] font-bold px-1.5 py-0.5 rounded-md ${TONE_CLASS[rolledUp.tone]}`}
              aria-label={rolledUp.label}
            >
              {rolledUp.count}
            </span>
          )}
        </button>
      </div>
      {/*
        Closed groups unmount their items rather than hiding them with the
        ``hidden`` attribute. Hiding leaves the item — and its badge's
        ``aria-label`` — in the DOM, so the rolled-up header badge and the
        hidden item badge both answer a label query. One "2 plans awaiting your
        input" must mean one element.

        The icon rail forces ``open``: a 64px strip has no room for group
        headers, so there would be nothing to expand and no way to reach a
        destination hidden inside a closed group.
      */}
      <div id={panelId}>
        {open &&
          group.items.map((item) => (
            <NavItem
              key={item.to}
              to={item.to}
              icon={item.icon}
              label={item.label}
              external={item.external}
              badge={item.badgeKey ? badges[item.badgeKey] : undefined}
              onNavigate={onNavigate}
            />
          ))}
      </div>
    </div>
  );
}
