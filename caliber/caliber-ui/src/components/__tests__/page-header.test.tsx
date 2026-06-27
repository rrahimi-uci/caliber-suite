import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { PageHeader } from "@/components/PageHeader";

describe("PageHeader", () => {
  it("lets subtitle content use the full available header width", () => {
    render(
      <MemoryRouter>
        <PageHeader
          title="Object Store"
          subtitle="Browse workspace buckets, inspect stored artifacts, and manage uploads from one place."
        />
      </MemoryRouter>,
    );

    const subtitle = screen.getByText(
      "Browse workspace buckets, inspect stored artifacts, and manage uploads from one place.",
    );
    expect(subtitle).toHaveClass("w-full");
    expect(subtitle).not.toHaveClass("max-w-2xl");
  });
});
