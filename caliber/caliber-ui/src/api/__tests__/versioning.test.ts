import { describe, expect, it } from "vitest";

import type { PromptVersionInfo } from "@/api/types";
import { liveVersion, promptVersionsToArtifactVersions } from "@/api/versioning";

function info(overrides: Partial<PromptVersionInfo>): PromptVersionInfo {
  return {
    name: "support-agent",
    version: 1,
    aliases: [],
    creation_timestamp: null,
    updated_timestamp: null,
    run_id: null,
    source: null,
    commit_message: null,
    current: false,
    ...overrides,
  };
}

describe("promptVersionsToArtifactVersions", () => {
  it("maps registry versions onto the normalized model and marks the live one", () => {
    const versions = promptVersionsToArtifactVersions("support-agent", [
      info({ version: 7, current: true, aliases: ["prod"], commit_message: "tighten" }),
      info({ version: 6, current: false, aliases: [] }),
    ]);

    expect(versions[0]).toMatchObject({
      artifactType: "prompt",
      versionKey: "7",
      versionLabel: "v7",
      ordinal: 7,
      status: "active",
      isLive: true,
      label: "tighten",
    });
    expect(versions[1]).toMatchObject({
      versionKey: "6",
      status: "published",
      isLive: false,
    });
  });

  it("derives isLive from the live alias when `current` is not set", () => {
    const versions = promptVersionsToArtifactVersions("p", [
      info({ version: 3, current: false, aliases: ["prod"] }),
    ]);
    expect(versions[0].isLive).toBe(true);
    expect(versions[0].status).toBe("active");
  });

  it("converts a numeric creation timestamp to an ISO string", () => {
    const versions = promptVersionsToArtifactVersions("p", [
      info({ version: 1, creation_timestamp: 0 }),
    ]);
    expect(versions[0].createdAt).toBe(new Date(0).toISOString());
  });

  it("liveVersion returns the version serving the live alias", () => {
    const versions = promptVersionsToArtifactVersions("p", [
      info({ version: 2, current: true, aliases: ["prod"] }),
      info({ version: 1 }),
    ]);
    expect(liveVersion(versions)?.versionKey).toBe("2");
  });
});
