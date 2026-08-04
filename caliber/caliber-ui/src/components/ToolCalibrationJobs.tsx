import { caliberApi } from "@/api/caliberApi";
import type { ToolCalibrationJob } from "@/api/workflowTypes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { relativeTime } from "@/lib/time";
import { useState } from "react";

export function ToolCalibrationJobs({
  toolId,
  canOperate,
}: {
  toolId: string;
  canOperate: boolean;
}): JSX.Element {
  const invalidate = useInvalidate();
  const [actionError, setActionError] = useState<string | null>(null);
  const jobsQuery = useApiQuery(
    ["tool-calibration-jobs", toolId],
    (signal) => caliberApi.listToolCalibrationJobs(toolId, signal),
    {
      refetchInterval: (query) =>
        query.state.data?.jobs.some((job) =>
          ["queued", "running"].includes(job.status),
        )
          ? 2000
          : false,
    },
  );
  const submit = useApiMutation(
    () => caliberApi.submitToolCalibrationJob(toolId),
    {
      onSuccess: () => invalidate(["tool-calibration-jobs", toolId]),
    },
  );
  const resolve = useApiMutation(
    (input: {
      job: ToolCalibrationJob;
      action: "retry" | "abandon";
      reason: string;
    }) =>
      caliberApi.resolveToolCalibrationJob(toolId, input.job.job_id, {
        action: input.action,
        reason: input.reason,
      }),
    { onSuccess: () => invalidate(["tool-calibration-jobs", toolId]) },
  );

  const run = async (action: () => Promise<unknown>): Promise<void> => {
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "Calibration action failed",
      );
    }
  };

  return (
    <section
      className="mt-3 rounded-md border border-surface-200 p-3"
      data-testid="tool-calibration-jobs"
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-700">
            Durable calibration jobs
          </h3>
          <p className="text-xs text-gray-500">
            Background runs survive browser disconnects; ambiguous executions
            require an operator decision.
          </p>
        </div>
        {canOperate && (
          <Button
            size="sm"
            disabled={submit.isPending}
            onClick={() => void run(() => submit.mutateAsync(undefined))}
          >
            Queue calibration
          </Button>
        )}
      </div>
      {(jobsQuery.error || actionError) && (
        <div role="alert" className="mt-2 text-xs text-red-700">
          {actionError ?? jobsQuery.error?.message}
        </div>
      )}
      {jobsQuery.data?.jobs.length === 0 && (
        <p className="mt-2 text-xs text-gray-400">
          No durable calibration jobs yet.
        </p>
      )}
      <ul className="mt-2 space-y-2">
        {jobsQuery.data?.jobs.map((job) => (
          <li
            key={job.job_id}
            data-testid={`tool-calibration-job-${job.job_id}`}
            className="flex flex-wrap items-center gap-2 text-xs"
          >
            <Badge
              variant={
                job.status === "completed"
                  ? "success"
                  : job.status === "failed"
                    ? "destructive"
                    : "warning"
              }
            >
              {job.status}
            </Badge>
            <span className="font-mono">{job.job_id}</span>
            {job.retry_of_job_id && <span>retry of {job.retry_of_job_id}</span>}
            {job.claimed_by && <span>claimed by {job.claimed_by}</span>}
            {job.created_at && <span>{relativeTime(job.created_at)}</span>}
            {job.pass_rate != null && (
              <span>{Math.round(job.pass_rate * 100)}% passed</span>
            )}
            {job.error && <span className="text-red-700">{job.error}</span>}
            {canOperate && job.status === "running" && (
              <span className="ml-auto flex gap-1">
                {(["retry", "abandon"] as const).map((action) => (
                  <Button
                    key={action}
                    size="sm"
                    variant={action === "abandon" ? "destructive" : "outline"}
                    onClick={() => {
                      const reason = window.prompt(
                        `Why should this ambiguous calibration be ${action === "retry" ? "retried" : "abandoned"}?`,
                      );
                      if (reason?.trim())
                        void run(() =>
                          resolve.mutateAsync({
                            job,
                            action,
                            reason: reason.trim(),
                          }),
                        );
                    }}
                  >
                    {action === "retry" ? "Retry as new job" : "Abandon"}
                  </Button>
                ))}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
