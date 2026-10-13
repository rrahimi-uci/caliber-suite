/**
 * The success line shown after a calibration candidate is applied.
 *
 * A server running the `fake` promoter (only reachable with
 * `CALIBER_ALLOW_SIMULATED_PROVIDERS`) records a promotion without changing the
 * registry and says so with `simulated: true`. Announcing "now live" for that
 * would be false, so a simulated apply gets its own wording and never claims a
 * version went live. A missing flag is read as a real release, which is what
 * older servers returned.
 */
export interface ApplyMessageInput {
  simulated?: boolean;
  artifactRef?: string | null;
  singleEnvironment: boolean;
  alias: string;
}

export function applySuccessMessage({
  simulated,
  artifactRef,
  singleEnvironment,
  alias,
}: ApplyMessageInput): string {
  const artifact = artifactRef ? ` Artifact: ${artifactRef}.` : "";
  if (simulated === true) {
    return (
      "Simulated apply: nothing was released. The server is running the fake promoter, " +
      "so the prompt registry was not changed and no new version is live." +
      artifact
    );
  }
  const effect = singleEnvironment
    ? "The promoted prompt version is now live."
    : `@${alias} now points to the promoted prompt version.`;
  return `Candidate applied. ${effect}${artifact}`;
}
