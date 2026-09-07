import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FormEvent } from "react";
import { describe, expect, it, vi } from "vitest";

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

  /**
   * These tests replace an earlier one that asserted
   * ``queryByRole("button")`` returned nothing. That assertion passed for the
   * wrong reason: ``aria-hidden`` had removed the control from the
   * accessibility tree while leaving it fully keyboard-operable — Tab reached
   * it and Enter fired its handler. The test measured a11y-tree presence and
   * was read as proving unreachability, so it hid the bypass instead of
   * catching it.
   *
   * What matters is behavioural, so that is what is asserted now: focus cannot
   * reach it, and activation does nothing even if focus somehow does.
   */
  it("keeps a blocked control out of the tab order", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">before</button>
        <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
          <button type="button">Publish</button>
        </MutationGuard>
        <button type="button">after</button>
      </>,
    );

    screen.getByRole("button", { name: "before" }).focus();
    await user.tab();

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "after" }));
  });

  it("does not activate a blocked control by keyboard, even when focused", async () => {
    // pointer-events: none stops the mouse and nothing else, so a focused
    // button would still fire on Enter or Space without the capture-phase
    // interception.
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
        <button type="button" onClick={onClick}>Publish</button>
      </MutationGuard>,
    );

    const inner = screen
      .getByTestId("mutation-guard-blocked")
      .querySelector("button")!;
    inner.focus(); // programmatic focus bypasses tabindex entirely

    await user.keyboard("{Enter}");
    await user.keyboard(" ");

    expect(onClick).not.toHaveBeenCalled();
  });

  it("does not activate a blocked control by click", () => {
    const onClick = vi.fn();
    render(
      <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
        <button type="button" onClick={onClick}>Publish</button>
      </MutationGuard>,
    );

    const inner = screen
      .getByTestId("mutation-guard-blocked")
      .querySelector("button")!;
    // fireEvent, not userEvent: userEvent honours pointer-events and would
    // refuse to dispatch, which proves the CSS works but not the handler guard.
    fireEvent.click(inner);

    expect(onClick).not.toHaveBeenCalled();
  });

  it("does not submit a form from inside a blocked control", () => {
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
          <button type="submit">Save</button>
        </MutationGuard>
      </form>,
    );

    const inner = screen
      .getByTestId("mutation-guard-blocked")
      .querySelector("button")!;
    fireEvent.click(inner);

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("announces the blocked state rather than leaving it silent", () => {
    // The control stays in the accessibility tree on purpose. Hiding it from
    // assistive technology is what made the bypass hard to see, and a user
    // who cannot perceive the action cannot learn why it is unavailable.
    render(
      <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
        <button type="button">Publish</button>
      </MutationGuard>,
    );

    const blocked = screen.getByTestId("mutation-guard-blocked");
    expect(blocked.querySelector('[aria-disabled="true"]')).not.toBeNull();
    expect(blocked).toHaveAttribute("data-blocked-reason", "Requires Operator access.");
    expect(screen.getByText("Requires Operator access.")).toBeInTheDocument();
  });

  it("returns the control to the tab order once access arrives", async () => {
    // A guard that parks tabindex has to put it back, or a control stays
    // unreachable after the user's scopes finish loading.
    const user = userEvent.setup();
    const { rerender } = render(
      <>
        <button type="button">before</button>
        <MutationGuard scopes={VIEWER} requires={[SCOPE_OPERATOR]}>
          <button type="button">Publish</button>
        </MutationGuard>
      </>,
    );

    rerender(
      <>
        <button type="button">before</button>
        <MutationGuard scopes={OPERATOR} requires={[SCOPE_OPERATOR]}>
          <button type="button">Publish</button>
        </MutationGuard>
      </>,
    );

    const publish = screen.getByRole("button", { name: "Publish" });
    expect(publish).not.toHaveAttribute("tabindex", "-1");
    screen.getByRole("button", { name: "before" }).focus();
    await user.tab();
    expect(document.activeElement).toBe(publish);
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
    // Present but inoperable — see "announces the blocked state" above.
    expect(
      screen.getByTestId("mutation-guard-blocked").querySelector("button"),
    ).toHaveAttribute("tabindex", "-1");
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
