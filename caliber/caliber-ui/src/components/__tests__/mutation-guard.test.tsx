import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MutationGuard } from "@/components/MutationGuard";
import { SCOPE_ADMIN, SCOPE_OPERATOR, SCOPE_VIEWER } from "@/lib/scopes";

const OPERATOR = [SCOPE_OPERATOR, SCOPE_VIEWER];
const VIEWER = [SCOPE_VIEWER];

describe("MutationGuard", () => {
  it("renders the control untouched when the caller is permitted", () => {
    render(
      <MutationGuard scopes={OPERATOR} requires={[SCOPE_OPERATOR]}>
        <button type="button">Publish</button>
      </MutationGuard>,
    );

    const button = screen.getByRole("button", { name: "Publish" });
    expect(button).toBeInTheDocument();
    expect(button).not.toBeDisabled();
    expect(screen.queryByTestId("mutation-guard-blocked")).not.toBeInTheDocument();
  });

  it("blocks and states the requirement when the caller lacks the scope", () => {
    render(
      <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
        <button type="button">Publish</button>
      </MutationGuard>,
    );

    // Disclose, don't hide: the user learns the requirement here rather than
    // from a 403 dialog after committing to the action.
    const blocked = screen.getByTestId("mutation-guard-blocked");
    expect(blocked).toBeInTheDocument();
    expect(screen.getByText("Requires Operator access.")).toBeInTheDocument();
  });

  it("removes a blocked control from the accessibility tree and the tab order", () => {
    render(
      <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
        <button type="button">Publish</button>
      </MutationGuard>,
    );

    // The button must not be reachable as an interactive control...
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    // ...but the reason must be, so the state is announced rather than silent.
    const blocked = screen.getByTestId("mutation-guard-blocked");
    expect(blocked.querySelector('[aria-disabled="true"]')).not.toBeNull();
    expect(blocked).toHaveAttribute("data-blocked-reason", "Requires Operator access.");
  });

  it("names every accepted scope when any one of several grants the action", () => {
    render(
      <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR, SCOPE_ADMIN]}>
        <button type="button">Promote</button>
      </MutationGuard>,
    );

    expect(
      screen.getByText("Requires Operator or Admin access."),
    ).toBeInTheDocument();
  });

  it("permits when the caller holds any one of several accepted scopes", () => {
    render(
      <MutationGuard scopes={OPERATOR} requires={[SCOPE_ADMIN, SCOPE_OPERATOR]}>
        <button type="button">Promote</button>
      </MutationGuard>,
    );

    expect(screen.getByRole("button", { name: "Promote" })).toBeInTheDocument();
  });

  it("renders nothing when hiding is explicitly requested", () => {
    render(
      <MutationGuard
        scopes={VIEWER}
        requires={[SCOPE_ADMIN]}
        hideWhenForbidden
      >
        <button type="button">Delete other user's session</button>
      </MutationGuard>,
    );

    expect(screen.queryByTestId("mutation-guard-blocked")).not.toBeInTheDocument();
    expect(screen.queryByText(/Requires/)).not.toBeInTheDocument();
  });

  it("blocks while identity is still loading, and says so", () => {
    // Never offer an action before authority is known — but distinguish
    // "checking" from "denied", which are different user situations.
    render(
      <MutationGuard scopes={undefined} requires={[SCOPE_OPERATOR]} loading>
        <button type="button">Publish</button>
      </MutationGuard>,
    );

    expect(screen.getByText("Checking your access…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
  });

  it("blocks an unloaded identity even without the loading flag", () => {
    render(
      <MutationGuard scopes={undefined} requires={[SCOPE_OPERATOR]}>
        <button type="button">Publish</button>
      </MutationGuard>,
    );

    expect(screen.getByTestId("mutation-guard-blocked")).toBeInTheDocument();
  });

  it("prefers a caller-supplied reason over the generated one", () => {
    render(
      <MutationGuard
        scopes={VIEWER}
        requires={[SCOPE_OPERATOR]}
        reason="Published versions are read-only. Restore as draft to edit."
      >
        <button type="button">Save</button>
      </MutationGuard>,
    );

    expect(
      screen.getByText("Published versions are read-only. Restore as draft to edit."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Requires Operator access.")).not.toBeInTheDocument();
  });

  it("blocks a viewer even when the requirement list is empty", () => {
    // A call site that forgot to state its requirement must not become an
    // open door.
    render(
      <MutationGuard scopes={VIEWER} requires={[]}>
        <button type="button">Do something</button>
      </MutationGuard>,
    );

    expect(screen.getByTestId("mutation-guard-blocked")).toBeInTheDocument();
    expect(screen.getByText("Not available.")).toBeInTheDocument();
  });
});
