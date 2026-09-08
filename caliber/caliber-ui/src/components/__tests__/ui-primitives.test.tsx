/**
 * Smoke tests for UI primitives.
 *
 * These validate that components render without crashing.
 */

import { describe, expect, it } from "vitest";

import { render, screen } from "@/test/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/* -------------------------------------------------------------------------- */
/* Component smoke tests                                                       */
/* -------------------------------------------------------------------------- */

describe("Button", () => {
  it("renders with text", () => {
    render(<Button>Click me</Button>);
    expect(
      screen.getByRole("button", { name: "Click me" }),
    ).toBeInTheDocument();
  });

  it("applies variant classes", () => {
    render(<Button variant="destructive">Delete</Button>);
    const btn = screen.getByRole("button", { name: "Delete" });
    expect(btn.className).toContain("bg-red-600");
  });

  it("disables when disabled prop is set", () => {
    render(<Button disabled>Disabled</Button>);
    expect(screen.getByRole("button", { name: "Disabled" })).toBeDisabled();
  });
});

describe("Input", () => {
  it("renders with placeholder", () => {
    render(<Input placeholder="Type here" />);
    expect(screen.getByPlaceholderText("Type here")).toBeInTheDocument();
  });
});

describe("Badge", () => {
  it("renders children", () => {
    render(<Badge>Active</Badge>);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });
});

describe("Label", () => {
  it("renders label text", () => {
    render(<Label>Name</Label>);
    expect(screen.getByText("Name")).toBeInTheDocument();
  });
});

describe("Tooltip", () => {
  // Controlled ``open`` rather than a hover interaction: Radix's real
  // hover-to-open timing is exactly the class of thing this project's own
  // vitest config calls out as intermittently flaky in jsdom. Forcing
  // ``open`` deterministically renders the portal content without racing
  // pointer/focus timers.
  it("renders trigger and content, merging a custom className onto the default styling", () => {
    render(
      <TooltipProvider>
        <Tooltip open>
          <TooltipTrigger>Hover me</TooltipTrigger>
          <TooltipContent className="custom-tip">Helpful text</TooltipContent>
        </Tooltip>
      </TooltipProvider>,
    );
    expect(screen.getByText("Hover me")).toBeInTheDocument();
    // Radix renders the visible content plus a visually-hidden accessible
    // duplicate (role="tooltip") -- both match this text, so assert on the
    // first (the real, visible content div carrying the merged className).
    const [content] = screen.getAllByText("Helpful text");
    expect(content).toBeInTheDocument();
    expect(content?.className).toContain("custom-tip");
    expect(content?.className).toContain("rounded-md");
  });

  it("renders no content when closed", () => {
    render(
      <TooltipProvider>
        <Tooltip open={false}>
          <TooltipTrigger>Hover me</TooltipTrigger>
          <TooltipContent>Helpful text</TooltipContent>
        </Tooltip>
      </TooltipProvider>,
    );
    expect(screen.getByText("Hover me")).toBeInTheDocument();
    expect(screen.queryByText("Helpful text")).not.toBeInTheDocument();
  });
});
