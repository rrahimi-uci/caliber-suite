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
 * `Placeholder` is the per-page stub for pages we haven't built yet.
 * Each is a self-contained "this lives here" card so the navigation is
 * exercised end-to-end while we iterate page-by-page.
 *
 * Pages are lazy-loaded via `React.lazy` so each route lands in its own
 * chunk. The `<Suspense>` fallback is a minimal loading skeleton. Each
 * route is wrapped in `<RouteErrorBoundary>` so a crash in one page
 * doesn't take down the entire SPA.
 */

import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import {
  AUTH_CHANGED_EVENT,
  clearLocalAuthSession,
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
const Settings = lazy(() => import("@/pages/Settings").then((m) => ({ default: m.Settings })));
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
    clearLocalAuthSession();
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
              <Route path="/tools" element={<ToolRegistry />} />
              <Route path="/tools/:toolId" element={<ToolDetail />} />
              <Route path="/mcp-servers" element={<McpServers />} />
              <Route path="/object-store" element={<ObjectStore />} />
              <Route path="/observability" element={<Observability />} />
              <Route path="/knowledge-bases" element={<KnowledgeBases />} />
              <Route path="/workflows" element={<Workflows />} />
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
              <Route path="*" element={<Placeholder title="Not found" />} />
            </Routes>
          </Suspense>
        </RouteErrorBoundary>
      </AppShell>
    </DashboardSummaryProvider>
  );
}

function Placeholder({ title }: { title: string }): JSX.Element {
  return (
    <div>
      <h1 className="text-xl font-semibold text-gray-900 mb-2">{title}</h1>
      <p className="text-sm text-gray-500">
        This page lands in a follow-up milestone. The route is wired so the
        sidebar navigation works end-to-end while we iterate.
      </p>
    </div>
  );
}
