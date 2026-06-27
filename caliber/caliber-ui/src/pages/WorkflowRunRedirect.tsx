import { Navigate, Link, useParams } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import { useApiQuery } from "@/hooks/useApiQuery";

export function WorkflowRunRedirect(): JSX.Element {
  const { runId } = useParams<{ runId: string }>();
  const normalizedRunId = runId?.trim() || "";
  const query = useApiQuery(
    ["workflow-run-redirect", normalizedRunId],
    (signal) => caliberApi.getWorkflowRun(normalizedRunId, signal),
    { enabled: normalizedRunId.length > 0 },
  );

  if (!normalizedRunId) {
    return <Navigate to="/workflows" replace />;
  }

  if (query.isError) {
    return (
      <div
        data-testid="workflow-run-redirect-error"
        className="rounded-2xl border border-red-200 bg-red-50/70 p-6 text-sm text-red-700 shadow-card"
      >
        <div className="font-semibold">
          Could not open workflow run{" "}
          <span className="font-mono">{normalizedRunId}</span>.
        </div>
        <div className="mt-2 text-red-600">
          {query.error.message}
        </div>
        <Link
          to="/workflows"
          className="mt-4 inline-flex rounded-xl border border-red-200 bg-white px-3 py-2 text-xs font-semibold text-red-700 transition-colors hover:border-red-300"
        >
          Back to workflows
        </Link>
      </div>
    );
  }

  if (query.isLoading || !query.data) {
    return (
      <div
        data-testid="workflow-run-redirect-loading"
        className="rounded-2xl border border-slate-200/70 bg-white p-6 text-sm text-slate-500 shadow-card"
      >
        Resolving workflow run <span className="font-mono text-slate-700">{normalizedRunId}</span>…
      </div>
    );
  }

  return (
    <Navigate
      replace
      to={`/workflows/${encodeURIComponent(query.data.workflow_id)}?tab=runs&run=${encodeURIComponent(query.data.workflow_run_id)}`}
    />
  );
}

export default WorkflowRunRedirect;
