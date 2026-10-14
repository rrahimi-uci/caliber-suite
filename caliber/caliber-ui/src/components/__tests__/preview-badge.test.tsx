import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { PageHeader } from "@/components/PageHeader";
import { PreviewBadge, PreviewBanner } from "@/components/PreviewBadge";
import { isPreviewDestination, PREVIEW_DESTINATIONS } from "@/lib/maturity";

describe("Preview labels", () => {
  it("renders one pill with an explanatory title", () => {
    render(<PreviewBadge />);
    const pill = screen.getByTestId("preview-badge");
    expect(pill).toHaveTextContent("Preview");
    expect(pill).toHaveAttribute("title", expect.stringContaining("outside the supported alpha journey"));
  });

  it("renders a banner with an optional page-specific sentence", () => {
    const { rerender } = render(<PreviewBanner />);
    expect(screen.getByRole("note")).toHaveTextContent("outside the supported alpha journey");

    rerender(<PreviewBanner>Needs an external server.</PreviewBanner>);
    expect(screen.getByRole("note")).toHaveTextContent("Needs an external server.");
  });

  it("puts the pill beside the page title without changing the heading's name", () => {
    render(
      <MemoryRouter>
        <PageHeader title="Cookbooks" preview />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Cookbooks" })).toBeInTheDocument();
    expect(screen.getByTestId("preview-badge")).toBeInTheDocument();
  });

  it("shows no pill on a page that is not preview", () => {
    render(
      <MemoryRouter>
        <PageHeader title="Prompts" />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("preview-badge")).not.toBeInTheDocument();
  });

  it("knows which destinations are preview", () => {
    expect(isPreviewDestination("/cookbooks")).toBe(true);
    expect(isPreviewDestination("/prompts")).toBe(false);
    expect(new Set(PREVIEW_DESTINATIONS).size).toBe(PREVIEW_DESTINATIONS.length);
  });
});
