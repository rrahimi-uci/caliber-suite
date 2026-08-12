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

import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, Route, Routes } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import {
  AUTH_CHANGED_EVENT,
  clearLocalAuthSession,
  createLocalAuthSession,
  getAuthEpoch,
  getStoredAuthSession,
  saveLocalAuthSession,
  signOut,
  type AuthChangedDetail,
  type LocalAuthSession,
} from "@/auth/localAuth";
import { DashboardSummaryProvider } from "@/components/DashboardSummaryContext";
import { RouteErrorBoundary } from "@/components/ErrorBoundary";
import { ProviderBanner } from "@/components/ProviderBanner";
import { AppShell } from "@/components/AppShell";
import { useDashboardSummary } from "@/hooks/useDashboardSummary";
import { Login } from "@/pages/Login";

const AuditLog = lazy(() =>
  import("@/pages/AuditLog").then((m) => ({ default: m.AuditLog })),
);
const Agents = lazy(() =>
  import("@/pages/Agents").then((m) => ({ default: m.Agents })),
);
const AgentDetail = lazy(() =>
  import("@/pages/AgentDetail").then((m) => ({ default: m.AgentDetail })),
);
const EvalDatasets = lazy(() =>
  import("@/pages/EvalDatasets").then((m) => ({ default: m.EvalDatasets })),
);
const EvalDatasetDetail = lazy(() =>
  import("@/pages/EvalDatasetDetail").then((m) => ({
    default: m.EvalDatasetDetail,
  })),
);
const Judges = lazy(() =>
  import("@/pages/Judges").then((m) => ({ default: m.Judges })),
);
const ReviewQueues = lazy(() =>
  import("@/pages/ReviewQueues").then((m) => ({ default: m.ReviewQueues })),
);
const AriaPlans = lazy(() =>
  import("@/pages/AriaPlans").then((m) => ({ default: m.AriaPlans })),
);
const Evaluations = lazy(() =>
  import("@/pages/Evaluations").then((m) => ({ default: m.Evaluations })),
);
const EvaluationDetail = lazy(() =>
  import("@/pages/EvaluationDetail").then((m) => ({
    default: m.EvaluationDetail,
  })),
);
const Gateway = lazy(() =>
  import("@/pages/Gateway").then((m) => ({ default: m.Gateway })),
);
const McpServers = lazy(() =>
  import("@/pages/McpServers").then((m) => ({ default: m.McpServers })),
);
const OpenApiIntegrations = lazy(() =>
  import("@/pages/OpenApiIntegrations").then((m) => ({
    default: m.OpenApiIntegrations,
  })),
);
const KnowledgeBases = lazy(() =>
  import("@/pages/KnowledgeBases").then((m) => ({ default: m.KnowledgeBases })),
);
const ObjectStore = lazy(() =>
  import("@/pages/ObjectStore").then((m) => ({ default: m.ObjectStore })),
);
const Observability = lazy(() =>
  import("@/pages/Observability").then((m) => ({ default: m.Observability })),
);
const Dashboard = lazy(() =>
  import("@/pages/Overview").then((m) => ({ default: m.Dashboard })),
);
const Prompts = lazy(() =>
  import("@/pages/Prompts").then((m) => ({ default: m.Prompts })),
);
const Releases = lazy(() =>
  import("@/pages/Releases").then((m) => ({ default: m.Releases })),
);
const Settings = lazy(() =>
  import("@/pages/Settings").then((m) => ({ default: m.Settings })),
);
const Administration = lazy(() =>
  import("@/pages/Administration").then((m) => ({ default: m.Administration })),
);
const Skills = lazy(() =>
  import("@/pages/Skills").then((m) => ({ default: m.Skills })),
);
const SkillDetail = lazy(() =>
  import("@/pages/SkillDetail").then((m) => ({ default: m.SkillDetail })),
);
const ToolRegistry = lazy(() =>
  import("@/pages/ToolRegistry").then((m) => ({ default: m.ToolRegistry })),
);
const ToolDetail = lazy(() =>
  import("@/pages/ToolDetail").then((m) => ({ default: m.ToolDetail })),
);
const WorkflowDetail = lazy(() =>
  import("@/pages/WorkflowDetail").then((m) => ({ default: m.WorkflowDetail })),
);
const WorkflowEditor = lazy(() =>
  import("@/pages/WorkflowEditor").then((m) => ({ default: m.WorkflowEditor })),
);
const WorkflowRunRedirect = lazy(() =>
  import("@/pages/WorkflowRunRedirect").then((m) => ({
    default: m.WorkflowRunRedirect,
  })),
);
const WorkflowVersionDetail = lazy(() =>
  import("@/pages/WorkflowVersionDetail").then((m) => ({
    default: m.WorkflowVersionDetail,
  })),
);
const Workflows = lazy(() =>
  import("@/pages/Workflows").then((m) => ({ default: m.Workflows })),
);
const Cookbooks = lazy(() =>
  import("@/pages/Cookbooks").then((m) => ({ default: m.Cookbooks })),
);

function PageLoader(): JSX.Element {
  return (
    <div className="flex items-center justify-center min-h-[40vh]">
      <div className="animate-pulse text-sm text-gray-400">Loading…</div>
    </div>
  );
}

export function App(): JSX.Element {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<LocalAuthSession | null>(() =>
    getStoredAuthSession(),
  );
  const sessionRef = useRef(session);
  const [authChecked, setAuthChecked] = useState(false);
  const [logoutPending, setLogoutPending] = useState(false);
  const [authNotice, setAuthNotice] = useState<string | null>(null);

  const transitionSession = useCallback(
    (next: LocalAuthSession | null): void => {
      const current = sessionRef.current;
      // Query results can contain account, secret, project, and other user-scoped
      // data. Compare canonical server user IDs, not display identities: `admin`
      // and `@admin` are distinct valid account IDs even though both display as
      // `@admin` in trusted-header mode.
      // A generation is the browser-session boundary. Even the same account can return
      // with different scopes/state after re-authentication, and component-local drafts or
      // tool outputs must not cross that boundary.
      if (current?.generation !== next?.generation) queryClient.clear();
      sessionRef.current = next;
      setSession(next);
    },
    [queryClient],
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const validationEpoch = getAuthEpoch();
    const validationIsCurrent = (): boolean =>
      getAuthEpoch() === validationEpoch;

    void caliberApi
      .getAuthSession(controller.signal)
      .then((info) => {
        // A storage/auth event may have installed or removed a different browser
        // generation while this request was in flight. Its response describes the old
        // cookie and must not overwrite or resurrect the newer local boundary.
        if (!active || !validationIsCurrent()) return;
        const authenticated =
          !info.login_required &&
          info.authenticated_by !== "none" &&
          info.user_id !== "anonymous";
        if (!authenticated) {
          clearLocalAuthSession();
          transitionSession(null);
          return;
        }

        const stored = getStoredAuthSession();
        // A validation request confirms the existing cookie; it is not a new login.
        // Preserve its generation so an unrelated tab reload cannot make an older
        // response look as though it belongs to a superseded login.
        const validated =
          stored?.username === info.user_id
            ? stored
            : createLocalAuthSession(info.user_id);
        if (validated !== stored) saveLocalAuthSession(validated);
        transitionSession(validated);
      })
      .catch((error: unknown) => {
        if (
          !active ||
          (error instanceof DOMException && error.name === "AbortError")
        ) {
          return;
        }
        if (!validationIsCurrent()) return;
        if (error instanceof ApiError && error.status === 401) {
          clearLocalAuthSession();
          transitionSession(null);
          return;
        }
        // A temporary network/server failure is not proof that a previously
        // validated cookie expired. Keep the display state and let requests
        // surface their own recoverable errors.
        transitionSession(getStoredAuthSession());
      })
      .finally(() => {
        if (active) setAuthChecked(true);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [transitionSession]);

  useEffect(() => {
    const sync = (event: Event): void => {
      if (event instanceof CustomEvent) {
        const detail = event.detail as AuthChangedDetail | undefined;
        if (typeof detail?.notice === "string") setAuthNotice(detail.notice);
      }
      transitionSession(getStoredAuthSession());
    };
    window.addEventListener(AUTH_CHANGED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(AUTH_CHANGED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, [transitionSession]);

  const onLogin = useCallback(() => {
    setAuthNotice(null);
    transitionSession(getStoredAuthSession());
  }, [transitionSession]);

  const onLogout = useCallback(async () => {
    // Revokes the session server-side, not just locally: clearing only the cookie
    // would leave a still-valid token in anything that captured it. Keep the login
    // form unavailable until the response settles, or a fast re-login can race the
    // delayed Set-Cookie deletion and local-state clear from this older logout.
    setLogoutPending(true);
    try {
      await signOut();
      // Normally signOut clears storage. If another tab completed a newer login
      // generation while this request was in flight, retain that newer identity.
      transitionSession(getStoredAuthSession());
    } finally {
      setLogoutPending(false);
    }
  }, [transitionSession]);

  if (!authChecked || logoutPending) return <PageLoader />;

  if (!session) {
    return (
      <Routes>
        <Route
          path="/login"
          element={<Login notice={authNotice} onLogin={onLogin} />}
        />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <AuthenticatedApp
      key={session.generation}
      session={session}
      onLogout={onLogout}
    />
  );
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
      <AppShell currentUser={session.username} onLogout={onLogout}>
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
              <Route
                path="/openapi-integrations"
                element={<OpenApiIntegrations />}
              />
              <Route path="/object-store" element={<ObjectStore />} />
              <Route path="/observability" element={<Observability />} />
              <Route path="/knowledge-bases" element={<KnowledgeBases />} />
              <Route path="/workflows" element={<Workflows />} />
              <Route path="/cookbooks" element={<Cookbooks />} />
              <Route path="/agents" element={<Agents />} />
              <Route path="/agents/:agentId" element={<AgentDetail />} />
              <Route
                path="/workflows/:workflowId/editor/:versionId"
                element={<WorkflowEditor />}
              />
              <Route
                path="/workflows/:workflowId"
                element={<WorkflowDetail />}
              />
              <Route
                path="/workflow-runs/:runId"
                element={<WorkflowRunRedirect />}
              />
              <Route
                path="/workflow-versions/:versionId"
                element={<WorkflowVersionDetail />}
              />
              <Route path="/skills" element={<Skills />} />
              <Route path="/skills/:skillId" element={<SkillDetail />} />
              <Route path="/eval-datasets" element={<EvalDatasets />} />
              <Route
                path="/eval-datasets/:datasetId"
                element={<EvalDatasetDetail />}
              />
              <Route path="/judges" element={<Judges />} />
              <Route path="/review-queues" element={<ReviewQueues />} />
              <Route path="/aria/plans" element={<AriaPlans />} />
              <Route path="/evaluations" element={<Evaluations />} />
              <Route
                path="/evaluations/:runId"
                element={<EvaluationDetail />}
              />
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
        This URL doesn&apos;t match any CALIBER page. Check the address, or pick
        a workspace from the sidebar.
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
