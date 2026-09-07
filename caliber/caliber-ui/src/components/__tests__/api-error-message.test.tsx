import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/caliberApi";
import { ApiErrorMessage } from "@/components/ApiErrorMessage";

function validationError(
  errors: Array<{ loc: (string | number)[]; msg: string; type: string }>,
): ApiError {
  const detail = "request body validation failed";
  return new ApiError(400, detail, { detail, status_code: 400, errors });
}

describe("ApiErrorMessage", () => {
  it("renders nothing when there is no error", () => {
    const { container } = render(<ApiErrorMessage error={null} />);
    expect(container).toBeEmptyDOMElement();

    const { container: undef } = render(<ApiErrorMessage error={undefined} />);
    expect(undef).toBeEmptyDOMElement();
  });

  it("shows the field detail the user needs, not just the generic summary", () => {
    // The reported gap: this rejection used to render as
    // "request body validation failed" and nothing else.
    render(
      <ApiErrorMessage
        error={validationError([
          { loc: ["category"], msg: "field required", type: "missing" },
          { loc: ["weight"], msg: "input should be a valid number", type: "float_type" },
        ])}
      />,
    );

    expect(screen.getByText("request body validation failed")).toBeInTheDocument();
    const fields = screen.getByTestId("api-error-fields");
    expect(fields).toHaveTextContent("Category");
    expect(fields).toHaveTextContent("Field required");
    expect(fields).toHaveTextContent("Weight");
    expect(fields).toHaveTextContent("Input should be a valid number");
    expect(fields.querySelectorAll("li")).toHaveLength(2);
  });

  it("announces itself, since the user has already moved focus by submitting", () => {
    render(<ApiErrorMessage error={validationError([
      { loc: ["name"], msg: "field required", type: "missing" },
    ])} />);

    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("exposes each failing field path for input-level marking", () => {
    render(
      <ApiErrorMessage
        error={validationError([
          { loc: ["criteria", 0, "weight"], msg: "field required", type: "missing" },
        ])}
      />,
    );

    const item = screen.getByTestId("api-error-fields").querySelector("li");
    expect(item).toHaveAttribute("data-field-path", "criteria.0.weight");
    expect(item).toHaveTextContent("Criteria › 1 › Weight");
  });

  it("renders a non-validation error as the summary alone", () => {
    render(
      <ApiErrorMessage
        error={new ApiError(404, "eval dataset 'ds-1' not found", {
          detail: "eval dataset 'ds-1' not found",
          status_code: 404,
        })}
      />,
    );

    expect(screen.getByText("eval dataset 'ds-1' not found")).toBeInTheDocument();
    expect(screen.queryByTestId("api-error-fields")).not.toBeInTheDocument();
  });

  it("records the status for styling and diagnostics", () => {
    render(<ApiErrorMessage error={new ApiError(403, "forbidden", null)} />);
    expect(screen.getByTestId("api-error")).toHaveAttribute(
      "data-error-status",
      "403",
    );
  });

  it("applies per-surface label overrides", () => {
    render(
      <ApiErrorMessage
        error={validationError([
          { loc: ["artifact_ref"], msg: "field required", type: "missing" },
        ])}
        labels={{ artifact_ref: "Artifact ID or registry name" }}
      />,
    );

    expect(
      screen.getByText("Artifact ID or registry name", { exact: false }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Artifact Ref")).not.toBeInTheDocument();
  });

  it("uses the caller's fallback for a non-API failure", () => {
    render(
      <ApiErrorMessage
        error={new Error("Failed to fetch")}
        fallback="Could not reach the server. Check your connection."
      />,
    );

    expect(
      screen.getByText("Could not reach the server. Check your connection."),
    ).toBeInTheDocument();
  });

  it("prefers the server's message over a fallback when the API answered", () => {
    render(
      <ApiErrorMessage
        error={new ApiError(409, "prompt 'x' is already promoted", null)}
        fallback="Could not save."
      />,
    );

    expect(screen.getByText("prompt 'x' is already promoted")).toBeInTheDocument();
    expect(screen.queryByText("Could not save.")).not.toBeInTheDocument();
  });

  it("accepts a custom test id so a surface can target its own banner", () => {
    render(
      <ApiErrorMessage
        error={new ApiError(400, "nope", null)}
        data-testid="prompt-edit-error"
      />,
    );

    expect(screen.getByTestId("prompt-edit-error")).toBeInTheDocument();
  });
});
