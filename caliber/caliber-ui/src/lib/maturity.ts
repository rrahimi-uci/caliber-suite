/**
 * Which areas of the app are labelled Preview in the alpha.
 *
 * One list, read by the sidebar and asserted against the scope decision in
 * `docs/caliber-alpha-gaps.md` section 2.2 ("LABEL preview": MCP servers, OpenAPI
 * integrations, cookbooks, and the Aria assistant and plans). Adding a destination here
 * puts the pill in the navigation; the page itself shows `PreviewBadge` and
 * `PreviewBanner` (alpha gap P1-10).
 *
 * Workspace releases and the release-candidate signoff factory are also preview, but they
 * are not navigation destinations: the first has no UI, the second is a labelled block on
 * the Releases page.
 *
 * Every destination here is also behind a server-side feature flag that is off by default
 * (``hooks/useFeature.ts``, ``caliber/features.py``): the sidebar lists it, and its route
 * renders, only when the deployment turned the flag on. The pill therefore describes what an
 * operator who opted in sees, not what a clean alpha install shows.
 */
export const PREVIEW_DESTINATIONS: readonly string[] = [
  "/cookbooks",
  "/aria/plans",
  "/mcp-servers",
  "/openapi-integrations",
];

export function isPreviewDestination(path: string): boolean {
  return PREVIEW_DESTINATIONS.includes(path);
}
