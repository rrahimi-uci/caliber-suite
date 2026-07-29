/**
 * Router + chrome.
 *
 * The dashboard summary is fetched at App scope (was: only by the
 * Dashboard page) so the sidebar badge counts populate regardless of
 * which route the user lands on directly. The same value flows down
 * to Dashboard via context, eliminating the duplicate fetch the old
 * arrangement would have caused. Live updates come from the
 * event-stream subscription inside :func:`useDashboardSummary`.
 *
 * `NotFound` is the wildcard route. It used to double as a stub for
 * unbuilt pages, but every navigable route now resolves to a real page,
 * so reaching it means the URL is genuinely wrong.
 *
 * Pages are lazy-loaded via `React.lazy` so each route lands in its own
 * chunk. The `<Suspense>` fallback is a minimal loading skeleton. Each
 * route is wrapped in `<RouteErrorBoundary>` so a crash in one page
 * doesn't take down the entire SPA.
 */

import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";

import {
  AUTH_CHANGED_EVENT,
  signOut,
  getStoredAuthSession,
  type LocalAuthSession,
} from "@/auth/localAuth";
import { DashboardSummaryProvider } from "@/components/DashboardSummaryContext";
import { RouteErrorBoundary } from "@/components/ErrorBoundary";
import { ProviderBanner } from "@/components/ProviderBanner";
import { AppShell } from "@/components/AppShell";
import { useDashboardSummary } from "@/hooks/useDashboardSummary";
import { Login } from "@/pages/Login";

const AuditLog = lazy(() => import("@/pages/AuditLog").then((m) => ({ default: m.AuditLog })));
const Agents = lazy(() => import("@/pages/Agents").then((m) => ({ default: m.Agents })));
const AgentDetail = lazy(() => import("@/pages/AgentDetail").then((m) => ({ default: m.AgentDetail })));
const EvalDatasets = lazy(() => import("@/pages/EvalDatasets").then((m) => ({ default: m.EvalDatasets })));
const EvalDatasetDetail = lazy(() => import("@/pages/EvalDatasetDetail").then((m) => ({ default: m.EvalDatasetDetail })));
const Judges = lazy(() => import("@/pages/Judges").then((m) => ({ default: m.Judges })));
const ReviewQueues = lazy(() => import("@/pages/ReviewQueues").then((m) => ({ default: m.ReviewQueues })));
const AriaPlans = lazy(() => import("@/pages/AriaPlans").then((m) => ({ default: m.AriaPlans })));
const Evaluations = lazy(() => import("@/pages/Evaluations").then((m) => ({ default: m.Evaluations })));
const EvaluationDetail = lazy(() => import("@/pages/EvaluationDetail").then((m) => ({ default: m.EvaluationDetail })));
const Gateway = lazy(() => import("@/pages/Gateway").then((m) => ({ default: m.Gateway })));
const McpServers = lazy(() => import("@/pages/McpServers").then((m) => ({ default: m.McpServers })));
const KnowledgeBases = lazy(() => import("@/pages/KnowledgeBases").then((m) => ({ default: m.KnowledgeBases })));
const ObjectStore = lazy(() => import("@/pages/ObjectStore").then((m) => ({ default: m.ObjectStore })));
const Observability = lazy(() => import("@/pages/Observability").then((m) => ({ default: m.Observability })));
const Dashboard = lazy(() => import("@/pages/Overview").then((m) => ({ default: m.Dashboard })));
const Prompts = lazy(() => import("@/pages/Prompts").then((m) => ({ default: m.Prompts })));
const Releases = lazy(() => import("@/pages/Releases").then((m) => ({ default: m.Releases })));
const Settings = lazy(() => import("@/pages/Settings").then((m) => ({ default: m.Settings })));
const Administration = lazy(() =>
  import("@/pages/Administration").then((m) => ({ default: m.Administration })),
);
const Skills = lazy(() => import("@/pages/Skills").then((m) => ({ default: m.Skills })));
const SkillDetail = lazy(() => import("@/pages/SkillDetail").then((m) => ({ default: m.SkillDetail })));
const ToolRegistry = lazy(() => import("@/pages/ToolRegistry").then((m) => ({ default: m.ToolRegistry })));
const ToolDetail = lazy(() => import("@/pages/ToolDetail").then((m) => ({ default: m.ToolDetail })));
const WorkflowDetail = lazy(() => import("@/pages/WorkflowDetail").then((m) => ({ default: m.WorkflowDetail })));
const WorkflowEditor = lazy(() => import("@/pages/WorkflowEditor").then((m) => ({ default: m.WorkflowEditor })));
const WorkflowRunRedirect = lazy(() => import("@/pages/WorkflowRunRedirect").then((m) => ({ default: m.WorkflowRunRedirect })));
const WorkflowVersionDetail = lazy(() => import("@/pages/WorkflowVersionDetail").then((m) => ({ default: m.WorkflowVersionDetail })));
const Workflows = lazy(() => import("@/pages/Workflows").then((m) => ({ default: m.Workflows })));

function PageLoader(): JSX.Element {
  return (
    <div className="flex items-center justify-center min-h-[40vh]">
      <div className="animate-pulse text-sm text-gray-400">Loading…</div>
    </div>
  );
}

export function App(): JSX.Element {
  const [session, setSession] = useState<LocalAuthSession | null>(() =>
    getStoredAuthSession(),
  );

  useEffect(() => {
    const sync = (): void => setSession(getStoredAuthSession());
    window.addEventListener(AUTH_CHANGED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(AUTH_CHANGED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const onLogin = useCallback(() => {
    setSession(getStoredAuthSession());
  }, []);

  const onLogout = useCallback(() => {
    // Revokes the session server-side, not just locally: clearing only the cookie
    // would leave a still-valid token in anything that captured it.
    void signOut();
    setSession(null);
  }, []);

  if (!session) {
    return (
      <Routes>
        <Route path="/login" element={<Login onLogin={onLogin} />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return <AuthenticatedApp session={session} onLogout={onLogout} />;
}

function AuthenticatedApp({
  session,
  onLogout,
}: {
  session: LocalAuthSession;
  onLogout: () => void;
}): JSX.Element {
  const summary = useDashboardSummary();

  return (
    <DashboardSummaryProvider value={summary}>
      <AppShell
        currentUser={session.username}
        onLogout={onLogout}
      >
        <RouteErrorBoundary>
          <ProviderBanner />
          <Suspense fallback={<PageLoader />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/login" element={<Navigate to="/" replace />} />
              <Route path="/prompts" element={<Prompts />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/administration" element={<Administration />} />
              <Route path="/tools" element={<ToolRegistry />} />
              <Route path="/tools/:toolId" element={<ToolDetail />} />
              <Route path="/mcp-servers" element={<McpServers />} />
              <Route path="/object-store" element={<ObjectStore />} />
              <Route path="/observability" element={<Observability />} />
              <Route path="/knowledge-bases" element={<KnowledgeBases />} />
              <Route path="/workflows" element={<Workflows />} />
              <Route path="/agents" element={<Agents />} />
              <Route path="/agents/:agentId" element={<AgentDetail />} />
              <Route path="/workflows/:workflowId/editor/:versionId" element={<WorkflowEditor />} />
              <Route path="/workflows/:workflowId" element={<WorkflowDetail />} />
              <Route path="/workflow-runs/:runId" element={<WorkflowRunRedirect />} />
              <Route path="/workflow-versions/:versionId" element={<WorkflowVersionDetail />} />
              <Route path="/skills" element={<Skills />} />
              <Route path="/skills/:skillId" element={<SkillDetail />} />
              <Route path="/eval-datasets" element={<EvalDatasets />} />
              <Route path="/eval-datasets/:datasetId" element={<EvalDatasetDetail />} />
              <Route path="/judges" element={<Judges />} />
              <Route path="/review-queues" element={<ReviewQueues />} />
              <Route path="/aria/plans" element={<AriaPlans />} />
              <Route path="/evaluations" element={<Evaluations />} />
              <Route path="/evaluations/:runId" element={<EvaluationDetail />} />
              <Route path="/gateway" element={<Gateway />} />
              <Route path="/audit-log" element={<AuditLog />} />
              <Route path="/releases" element={<Releases />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </RouteErrorBoundary>
      </AppShell>
    </DashboardSummaryProvider>
  );
}

/**
 * Wildcard route.
 *
 * Previously this rendered "This page lands in a follow-up milestone",
 * which read as "CALIBER hasn't built this yet" for what is in fact an
 * unrecognised URL — a mistyped link looked like a missing feature.
 */
function NotFound(): JSX.Element {
  return (
    <div data-testid="route-not-found">
      <h1 className="text-xl font-semibold text-gray-900 mb-2 dark:text-slate-100">
        Page not found
      </h1>
      <p className="text-sm text-gray-500 dark:text-slate-400">
        This URL doesn&apos;t match any CALIBER page. Check the address, or
        pick a workspace from the sidebar.
      </p>
      <Link
        to="/"
        className="mt-4 inline-block text-sm font-medium text-caliber-600 hover:underline dark:text-caliber-400"
      >
        Go to the dashboard
      </Link>
    </div>
  );
}
