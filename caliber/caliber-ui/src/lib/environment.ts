// Single-environment mode for the initial product version.
//
// CALIBER's full model promotes artifacts through a dev → staging → prod alias
// ladder with eval-gated, human-approved promotion. For v1 we collapse that to
// a single environment: a build deploys immediately to one live alias, with no
// stage selector and no promotion-approval queue.
//
// This is a UI-level collapse. The backend promotion machinery is left intact
// but dormant (see GATED_ALIASES in caliber/workflows/promoter.py, which is now
// empty). Flipping SINGLE_ENVIRONMENT back to false here — and re-adding the
// gated alias on the backend — restores the full ladder without code surgery.
export const SINGLE_ENVIRONMENT = true;

// The one alias every build deploys to. Kept as "prod" because that is the
// alias the runtime resolves artifacts from (artifact_store, prompt registry),
// so already-deployed prompts/workflows keep resolving. In single-environment
// mode this string is an implementation detail — never surface it to users;
// show neutral wording like "Live" / "Deployed" instead.
export const LIVE_ALIAS = "prod";

// Alias choices offered in selectors / autocomplete hints. Collapses to the
// single live alias in single-environment mode.
export const DEPLOYMENT_ALIASES: readonly string[] = SINGLE_ENVIRONMENT
  ? [LIVE_ALIAS]
  : ["dev", "staging", "prod"];
