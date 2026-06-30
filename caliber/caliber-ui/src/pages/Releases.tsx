/**
 * Releases & Rollback — the cross-artifact "what's live, and what changed?" hub.
 *
 * A read-only control-plane view: a board of what's currently live across
 * artifacts, and a unified promotion/rollback/activation timeline. Per-artifact
 * rollback lives on each artifact's page; this aggregates the picture.
 */
import { caliberApi } from "@/api/caliberApi";
import type { ReleaseTimelineEvent } from "@/api/versioning";
import { Badge } from "@/components/ui/badge";
import { useApiQuery } from "@/hooks/useApiQuery";
import { relativeTime } from "@/lib/time";

const ACTION_LABEL: Record<string, string> = {
  promote_prompt: "Promote",
  rollback_prompt: "Roll back",
  promote_workflow: "Promote",
  rollback_workflow: "Roll back",
  activate_knowledge_base_version: "Activate",
  rollback_knowledge_base_version: "Roll back",
  rollback_skill: "Roll back",
};

function isRollback(action: string): boolean {
  return action.startsWith("rollback");
}

function transition(event: ReleaseTimelineEvent): string {
  const d = event.details ?? {};
  const from = d.from_version ?? d.previous_active_version_id ?? null;
  const to = d.to_version ?? d.version_id ?? null;
  if (from != null && to != null) return `v${from} → v${to}`;
  if (to != null) return `→ v${to}`;
  return "";
}

export function Releases(): JSX.Element {
  const liveQuery = useApiQuery(["releases-live"], () => caliberApi.listReleasesLive());
  const timelineQuery = useApiQuery(["releases-timeline"], () =>
    caliberApi.listReleasesTimeline({ limit: 100 }),
  );

  return (
    <div className="space-y-6 p-6" data-testid="releases-page">
      <div>
        <h1 className="text-xl font-semibold text-gray-900">Releases &amp; Rollback</h1>
        <p className="mt-0.5 text-sm text-gray-500">
          What&apos;s live across artifacts, and the recent promotion/rollback history.
        </p>
      </div>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-700">What&apos;s live now</h2>
        <div
          data-testid="releases-live-board"
          className="overflow-hidden rounded-md border border-surface-200"
        >
          {liveQuery.isLoading && <div className="p-3 text-sm text-gray-400">Loading…</div>}
          {liveQuery.data && liveQuery.data.length === 0 && (
            <div className="p-3 text-sm text-gray-400">Nothing deployed yet.</div>
          )}
          {liveQuery.data?.map((row) => (
            <div
              key={`${row.artifact_type}:${row.artifact_id}`}
              data-testid={`releases-live-${row.artifact_id}`}
              className="flex items-center gap-3 border-b border-surface-100 px-3 py-2 text-sm last:border-0"
            >
              <Badge variant="secondary">{row.artifact_type}</Badge>
              <span className="font-medium text-gray-900">
                {row.artifact_name ?? row.artifact_id}
              </span>
              <span className="font-mono text-xs text-gray-500">{row.version_id}</span>
              <span className="ml-auto text-xs text-gray-400">
                {row.since ? `since ${relativeTime(row.since)}` : ""}
                {row.by ? ` · ${row.by}` : ""}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-700">Timeline</h2>
        <ul data-testid="releases-timeline" className="space-y-1">
          {timelineQuery.isLoading && <li className="text-sm text-gray-400">Loading…</li>}
          {timelineQuery.data && timelineQuery.data.length === 0 && (
            <li className="text-sm text-gray-400">No promotions or rollbacks yet.</li>
          )}
          {timelineQuery.data?.map((event) => {
            const overridden = Boolean(event.details?.overridden);
            return (
              <li
                key={event.log_id}
                data-testid={`releases-event-${event.log_id}`}
                className="flex items-center gap-2 rounded border border-surface-100 px-3 py-1.5 text-sm"
              >
                <Badge variant={isRollback(event.action) ? "warning" : "default"}>
                  {ACTION_LABEL[event.action] ?? event.action}
                </Badge>
                <span className="text-gray-600">{event.entity_type}</span>
                <span className="font-medium text-gray-900">{event.entity_id}</span>
                <span className="font-mono text-xs text-gray-500">{transition(event)}</span>
                {overridden && (
                  <Badge variant="destructive" data-testid={`releases-overridden-${event.log_id}`}>
                    gate overridden
                  </Badge>
                )}
                <span className="ml-auto text-xs text-gray-400">
                  {event.actor}
                  {event.timestamp ? ` · ${relativeTime(event.timestamp)}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
