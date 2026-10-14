import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ToolOriginBadges,
  ToolUnavailableReason,
} from "@/components/ToolOriginBadges";

const REASON =
  "Agent memory is off. Set CALIBER_MEMORY_ENABLED=true and restart CALIBER.";

describe("ToolOriginBadges", () => {
  it("marks a seeded tool as built-in", () => {
    render(
      <ToolOriginBadges tool={{ builtin: true, availability: "available" }} />,
    );
    expect(screen.getByTestId("tool-builtin-badge")).toHaveTextContent(
      "Built-in",
    );
    expect(
      screen.queryByTestId("tool-unavailable-badge"),
    ).not.toBeInTheDocument();
  });

  it("marks a tool that cannot do anything right now, and says why on hover", () => {
    render(
      <ToolOriginBadges
        tool={{
          builtin: true,
          availability: "unavailable",
          availability_reason: REASON,
        }}
      />,
    );
    expect(screen.getByTestId("tool-builtin-badge")).toBeInTheDocument();
    expect(screen.getByTestId("tool-unavailable-badge")).toHaveAttribute(
      "title",
      REASON,
    );
  });

  it("renders nothing for an ordinary registered tool", () => {
    const { container } = render(
      <ToolOriginBadges tool={{ builtin: false, availability: "available" }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when an older server sends neither field", () => {
    const { container } = render(<ToolOriginBadges tool={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("flags an unavailable tool even when it is not built in", () => {
    render(
      <ToolOriginBadges
        tool={{ builtin: false, availability: "unavailable" }}
      />,
    );
    expect(screen.queryByTestId("tool-builtin-badge")).not.toBeInTheDocument();
    expect(screen.getByTestId("tool-unavailable-badge")).toHaveAttribute(
      "title",
      "Unavailable",
    );
  });
});

describe("ToolUnavailableReason", () => {
  it("shows the reason only for an unavailable tool that has one", () => {
    const { rerender } = render(
      <ToolUnavailableReason
        tool={{ availability: "unavailable", availability_reason: REASON }}
      />,
    );
    expect(screen.getByTestId("tool-unavailable-reason")).toHaveTextContent(
      REASON,
    );

    rerender(
      <ToolUnavailableReason
        tool={{ availability: "available", availability_reason: REASON }}
      />,
    );
    expect(
      screen.queryByTestId("tool-unavailable-reason"),
    ).not.toBeInTheDocument();

    rerender(
      <ToolUnavailableReason
        tool={{ availability: "unavailable", availability_reason: null }}
      />,
    );
    expect(
      screen.queryByTestId("tool-unavailable-reason"),
    ).not.toBeInTheDocument();
  });
});
