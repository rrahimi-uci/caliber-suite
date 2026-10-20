import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { ToolDetail } from "@/pages/ToolDetail";

function Probe(): JSX.Element {
  const location = useLocation();
  return <div data-testid="at">{`${location.pathname}${location.search}`}</div>;
}

describe("ToolDetail", () => {
  it("is the workspace now: /tools/:id redirects to /tools?tool=:id, replacing the history entry", () => {
    render(
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/tools", "/tools/TL-7"]}
        initialIndex={1}
      >
        <Routes>
          <Route path="/tools/:toolId" element={<ToolDetail />} />
          <Route path="/tools" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    // Every link and bookmark made while the detail page existed lands in the one tool page.
    expect(screen.getByTestId("at")).toHaveTextContent("/tools?tool=TL-7");
  });

  it("escapes the id it forwards", () => {
    render(
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/tools/a%26b"]}
      >
        <Routes>
          <Route path="/tools/:toolId" element={<ToolDetail />} />
          <Route path="/tools" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("at")).toHaveTextContent("/tools?tool=a%26b");
  });
});
