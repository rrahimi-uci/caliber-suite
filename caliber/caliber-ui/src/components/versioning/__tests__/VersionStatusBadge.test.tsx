import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LiveBadge, VersionStatusBadge } from "@/components/versioning/VersionStatusBadge";
import type { VersionStatus } from "@/api/versioning";

describe("VersionStatusBadge", () => {
  it.each<[VersionStatus, string]>([
    ["draft", "Draft"],
    ["published", "Published"],
    ["active", "Active"],
    ["deprecated", "Deprecated"],
    ["archived", "Archived"],
  ])("renders %s with a distinct label + testid", (status, label) => {
    render(<VersionStatusBadge status={status} />);
    expect(screen.getByTestId(`version-status-${status}`)).toHaveTextContent(label);
  });
});

describe("LiveBadge", () => {
  it("reads LIVE in single-environment (prod) mode", () => {
    render(<LiveBadge alias="prod" />);
    expect(screen.getByTestId("version-live-badge")).toHaveTextContent("LIVE");
  });

  it("reads @alias for a non-prod alias", () => {
    render(<LiveBadge alias="staging" />);
    expect(screen.getByTestId("version-live-badge")).toHaveTextContent("@staging");
  });
});
