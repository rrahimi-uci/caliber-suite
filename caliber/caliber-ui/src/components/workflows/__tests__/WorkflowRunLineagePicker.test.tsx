import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WorkflowRunLineagePicker } from "@/components/workflows/WorkflowRunLineagePicker";
import type { WorkflowRunLineageOption } from "@/lib/workflowRunLineage";

const options: WorkflowRunLineageOption[] = [
  {
    workspace_release_id: "release-dev",
    environment_id: "env-dev",
    release_label: "release-dev",
    environment_label: "Development",
    environment_class: "dev",
    promotion_order: 1,
  },
  {
    workspace_release_id: "release-prod",
    environment_id: "env-prod",
    release_label: "release-prod",
    environment_label: "Production",
    environment_class: "prod",
    promotion_order: 2,
  },
];

describe("WorkflowRunLineagePicker", () => {
  it("renders accessible release/environment labels and selects a complete pair", () => {
    const onSelect = vi.fn();
    render(
      <WorkflowRunLineagePicker
        options={options}
        selectedWorkspaceReleaseId="release-dev"
        selectedEnvironmentId="env-dev"
        onSelect={onSelect}
        isLoading={false}
        error={null}
      />,
    );

    expect(screen.getByLabelText("Approved workspace release")).toHaveValue(
      "release-dev",
    );
    expect(screen.getByLabelText("Approved runtime environment")).toHaveValue(
      "env-dev",
    );
    expect(screen.getByText("release-dev · Development")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Approved workspace release"), {
      target: { value: "release-prod" },
    });
    expect(onSelect).toHaveBeenCalledWith(options[1]);
    fireEvent.change(screen.getByLabelText("Approved runtime environment"), {
      target: { value: "env-prod" },
    });
    expect(onSelect).toHaveBeenLastCalledWith(options[1]);
    fireEvent.change(screen.getByLabelText("Approved workspace release"), {
      target: { value: "missing-release" },
    });
    fireEvent.change(screen.getByLabelText("Approved runtime environment"), {
      target: { value: "missing-environment" },
    });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByTestId("workflow-run-lineage-state"),
    ).not.toBeInTheDocument();
  });

  it("renders the exact lineage refusal and disables both selectors when no pair is approved", () => {
    render(
      <WorkflowRunLineagePicker
        options={[]}
        selectedWorkspaceReleaseId={null}
        selectedEnvironmentId={null}
        onSelect={vi.fn()}
        isLoading={false}
        error={null}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "lineage_required: no approved workspace release and environment is available",
    );
    expect(screen.getByLabelText("Approved workspace release")).toBeDisabled();
    expect(
      screen.getByLabelText("Approved runtime environment"),
    ).toBeDisabled();
  });

  it("shows loading and incomplete-selection states with the appropriate disabled controls", () => {
    const { rerender } = render(
      <WorkflowRunLineagePicker
        options={options}
        selectedWorkspaceReleaseId={null}
        selectedEnvironmentId={null}
        onSelect={vi.fn()}
        isLoading
        error={null}
        compact
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Loading approved workspace releases and environments",
    );
    expect(screen.getByLabelText("Approved workspace release")).toBeDisabled();
    expect(
      screen.getByLabelText("Approved runtime environment"),
    ).toBeDisabled();

    rerender(
      <WorkflowRunLineagePicker
        options={options}
        selectedWorkspaceReleaseId={null}
        selectedEnvironmentId={null}
        onSelect={vi.fn()}
        isLoading={false}
        error={null}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "lineage_required: select an approved workspace release and environment",
    );
  });

  it("surfaces server loading errors without changing the selected pair", () => {
    const onSelect = vi.fn();
    render(
      <WorkflowRunLineagePicker
        options={[options[0]!]}
        selectedWorkspaceReleaseId="release-dev"
        selectedEnvironmentId="env-dev"
        onSelect={onSelect}
        isLoading={false}
        error={new Error("feature_disabled") as never}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("feature_disabled");
    expect(screen.getByLabelText("Approved workspace release")).toHaveValue(
      "release-dev",
    );
    expect(onSelect).not.toHaveBeenCalled();
  });
});
