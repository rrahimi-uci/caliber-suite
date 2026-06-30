import { Badge, type BadgeProps } from "@/components/ui/badge";
import type { VersionStatus } from "@/api/versioning";

/**
 * A dedicated badge for the five normalized version statuses.
 *
 * The shared `StatusBadge` only knows queue/job/approval vocabulary, so every
 * version status would fall through to an undifferentiated gray. This maps each
 * status to its own tone + label.
 */
const STATUS_VARIANT: Record<VersionStatus, NonNullable<BadgeProps["variant"]>> = {
  draft: "secondary",
  published: "outline",
  active: "success",
  deprecated: "warning",
  archived: "secondary",
};

const STATUS_LABEL: Record<VersionStatus, string> = {
  draft: "Draft",
  published: "Published",
  active: "Active",
  deprecated: "Deprecated",
  archived: "Archived",
};

export function VersionStatusBadge({ status }: { status: VersionStatus }): JSX.Element {
  return (
    <Badge variant={STATUS_VARIANT[status]} data-testid={`version-status-${status}`}>
      {STATUS_LABEL[status]}
    </Badge>
  );
}

/**
 * The single source of "what is serving" in a version list. In single-environment
 * mode it reads "LIVE"; with the multi-stage ladder restored it reads "@{alias}".
 */
export function LiveBadge({ alias }: { alias?: string }): JSX.Element {
  return (
    <Badge
      variant="success"
      data-testid="version-live-badge"
      className="gap-1"
    >
      <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-green-600" />
      {alias && alias !== "prod" ? `@${alias}` : "LIVE"}
    </Badge>
  );
}
