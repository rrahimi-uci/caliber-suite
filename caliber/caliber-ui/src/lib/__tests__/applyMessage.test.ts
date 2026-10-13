import { describe, expect, it } from "vitest";

import { applySuccessMessage } from "@/lib/applyMessage";

describe("applySuccessMessage", () => {
  it("announces a real release as live in a single-environment install", () => {
    expect(
      applySuccessMessage({
        simulated: false,
        artifactRef: "prompts:/support/4",
        singleEnvironment: true,
        alias: "prod",
      }),
    ).toBe(
      "Candidate applied. The promoted prompt version is now live. Artifact: prompts:/support/4.",
    );
  });

  it("names the alias when environments are separate", () => {
    expect(
      applySuccessMessage({ singleEnvironment: false, alias: "staging" }),
    ).toBe(
      "Candidate applied. @staging now points to the promoted prompt version.",
    );
  });

  it("reads a missing flag as a real release (older servers)", () => {
    expect(
      applySuccessMessage({ singleEnvironment: true, alias: "prod" }),
    ).toContain("now live");
  });

  it("never claims a simulated apply is live", () => {
    const message = applySuccessMessage({
      simulated: true,
      artifactRef: "prompt://support/v-fake",
      singleEnvironment: true,
      alias: "prod",
    });
    expect(message).toContain("Simulated apply: nothing was released");
    expect(message).toContain("no new version is live");
    expect(message).toContain("Artifact: prompt://support/v-fake.");
    expect(message).not.toContain("is now live");
    expect(message).not.toContain("Candidate applied");
  });
});
