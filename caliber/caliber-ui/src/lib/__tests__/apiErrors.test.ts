import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/caliberApi";
import {
  apiErrorText,
  describeApiError,
  invalidFieldPaths,
} from "@/lib/apiErrors";

/**
 * Builds the exact body ``validation_exception_handler`` returns
 * (caliber/src/caliber/routes/_errors.py) so these tests exercise the real
 * contract rather than an idealised one.
 */
function validationError(
  errors: Array<{ loc: (string | number)[]; msg: string; type: string }>,
): ApiError {
  const detail = "request body validation failed";
  return new ApiError(400, detail, { detail, status_code: 400, errors });
}

describe("describeApiError — structured validation failures", () => {
  it("keeps the summary and surfaces every field message", () => {
    const described = describeApiError(
      validationError([
        { loc: ["category"], msg: "field required", type: "missing" },
        { loc: ["weight"], msg: "input should be a valid number", type: "float_type" },
      ]),
    );

    expect(described.summary).toBe("request body validation failed");
    expect(described.status).toBe(400);
    expect(described.issues).toHaveLength(2);
    expect(described.issues[0]).toMatchObject({
      path: "category",
      label: "Category",
      message: "Field required",
    });
    expect(described.issues[1]).toMatchObject({
      path: "weight",
      label: "Weight",
      message: "Input should be a valid number",
    });
  });

  it("humanises snake_case and kebab-case field names", () => {
    const described = describeApiError(
      validationError([
        { loc: ["artifact_ref"], msg: "field required", type: "missing" },
        { loc: ["required-score"], msg: "field required", type: "missing" },
      ]),
    );

    expect(described.issues.map((i) => i.label)).toEqual([
      "Artifact Ref",
      "Required Score",
    ]);
  });

  it("renders a nested path as a readable trail with one-based indices", () => {
    // `items.0.name` is the *first* item to a human, not the zeroth.
    const described = describeApiError(
      validationError([
        { loc: ["criteria", 0, "weight"], msg: "field required", type: "missing" },
        { loc: ["criteria", "2", "title"], msg: "field required", type: "missing" },
      ]),
    );

    expect(described.issues[0].label).toBe("Criteria › 1 › Weight");
    expect(described.issues[0].path).toBe("criteria.0.weight");
    expect(described.issues[1].label).toBe("Criteria › 3 › Title");
  });

  it("drops the request-location prefix a user cannot act on", () => {
    const described = describeApiError(
      validationError([
        { loc: ["body", "name"], msg: "field required", type: "missing" },
        { loc: ["query", "limit"], msg: "input should be a valid integer", type: "int_type" },
      ]),
    );

    expect(described.issues[0].label).toBe("Name");
    expect(described.issues[0].path).toBe("name");
    expect(described.issues[1].label).toBe("Limit");
  });

  it("de-duplicates the repeats a union failure produces", () => {
    // Pydantic emits one entry per failing union member; a single user mistake
    // must not read as three problems.
    const described = describeApiError(
      validationError([
        { loc: ["target"], msg: "field required", type: "missing" },
        { loc: ["target"], msg: "field required", type: "missing" },
        { loc: ["target"], msg: "input should be a valid string", type: "string_type" },
      ]),
    );

    expect(described.issues).toHaveLength(2);
    expect(described.issues.map((i) => i.message)).toEqual([
      "Field required",
      "Input should be a valid string",
    ]);
  });

  it("honours a per-surface label override", () => {
    const described = describeApiError(
      validationError([{ loc: ["artifact_ref"], msg: "field required", type: "missing" }]),
      { artifact_ref: "Artifact ID or registry name" },
    );

    expect(described.issues[0].label).toBe("Artifact ID or registry name");
  });

  it("handles a whole-object error with no field to name", () => {
    const described = describeApiError(
      validationError([
        { loc: ["__root__"], msg: "at least one criterion is required", type: "value_error" },
      ]),
    );

    expect(described.issues[0].label).toBe("");
    expect(described.issues[0].message).toBe("At least one criterion is required");
  });

  it("skips malformed entries instead of rendering blanks", () => {
    const described = describeApiError(
      new ApiError(400, "request body validation failed", {
        detail: "request body validation failed",
        status_code: 400,
        errors: [
          { loc: ["good"], msg: "field required", type: "missing" },
          // Shapes that should never occur, but must not produce empty rows.
          { loc: ["bad"], msg: "   ", type: "missing" },
          null as never,
          "nonsense" as never,
        ],
      }),
    );

    expect(described.issues).toHaveLength(1);
    expect(described.issues[0].label).toBe("Good");
  });
});

describe("describeApiError — everything that is not a schema failure", () => {
  it("passes a 404 through as its own message with no invented structure", () => {
    const described = describeApiError(
      new ApiError(404, "eval dataset 'ds-1' not found", {
        detail: "eval dataset 'ds-1' not found",
        status_code: 404,
      }),
    );

    expect(described.summary).toBe("eval dataset 'ds-1' not found");
    expect(described.issues).toEqual([]);
    expect(described.status).toBe(404);
  });

  it("passes a 403 through unchanged", () => {
    const described = describeApiError(
      new ApiError(403, "requires caliber.operator", {
        detail: "requires caliber.operator",
        status_code: 403,
      }),
    );

    expect(described.summary).toBe("requires caliber.operator");
    expect(described.issues).toEqual([]);
  });

  it("survives an ApiError whose body never parsed", () => {
    const described = describeApiError(new ApiError(502, "Bad Gateway", null));

    expect(described.summary).toBe("Bad Gateway");
    expect(described.issues).toEqual([]);
    expect(described.status).toBe(502);
  });

  it("falls back for a plain Error, reporting no status", () => {
    const described = describeApiError(new Error("Failed to fetch"));

    expect(described.summary).toBe("Failed to fetch");
    expect(described.status).toBeNull();
  });

  it("never renders an empty summary", () => {
    expect(describeApiError(new Error("")).summary).toBe(
      "Something went wrong. Please try again.",
    );
    expect(describeApiError(undefined).summary).toBe(
      "Something went wrong. Please try again.",
    );
    expect(describeApiError("a thrown string").summary).toBe(
      "Something went wrong. Please try again.",
    );
    expect(
      describeApiError(new ApiError(500, "   ", null)).summary,
    ).toBe("The request failed.");
  });

  it("does not leak anything but the server's own strings", () => {
    // A body carrying extra server-side keys must contribute nothing.
    const described = describeApiError(
      new ApiError(500, "internal error", {
        detail: "internal error",
        status_code: 500,
        // @ts-expect-error — deliberately exercising an unexpected body shape
        traceback: "File \"/app/routes.py\", line 42",
      }),
    );

    expect(JSON.stringify(described)).not.toContain("routes.py");
    expect(described.issues).toEqual([]);
  });
});

describe("apiErrorText", () => {
  it("appends field detail to the summary on one line", () => {
    const text = apiErrorText(
      validationError([
        { loc: ["category"], msg: "field required", type: "missing" },
        { loc: ["weight"], msg: "input should be a valid number", type: "float_type" },
      ]),
    );

    expect(text).toBe(
      "request body validation failed — Category: Field required; Weight: Input should be a valid number",
    );
  });

  it("returns the summary alone when there is no field detail", () => {
    expect(
      apiErrorText(new ApiError(404, "prompt 'x' not found", null)),
    ).toBe("prompt 'x' not found");
  });

  it("uses the caller's fallback only for non-Error throws", () => {
    // A real Error's own message beats a generic fallback; a thrown
    // non-Error has nothing better to offer.
    expect(apiErrorText("boom", "Failed to save prompt changes")).toBe(
      "Failed to save prompt changes",
    );
    expect(apiErrorText(new Error("Network down"), "Failed to save")).toBe(
      "Network down",
    );
  });

  it("applies label overrides", () => {
    expect(
      apiErrorText(
        validationError([{ loc: ["artifact_ref"], msg: "field required", type: "missing" }]),
        undefined,
        { artifact_ref: "Artifact ID" },
      ),
    ).toContain("Artifact ID: Field required");
  });
});

describe("invalidFieldPaths", () => {
  it("lists the dotted paths that failed", () => {
    const paths = invalidFieldPaths(
      validationError([
        { loc: ["body", "name"], msg: "field required", type: "missing" },
        { loc: ["criteria", 0, "weight"], msg: "field required", type: "missing" },
      ]),
    );

    expect(paths).toEqual(new Set(["name", "criteria.0.weight"]));
  });

  it("omits whole-object errors that name no field", () => {
    const paths = invalidFieldPaths(
      validationError([{ loc: ["__root__"], msg: "invalid", type: "value_error" }]),
    );

    expect(paths.size).toBe(0);
  });

  it("is empty for a non-validation error", () => {
    expect(invalidFieldPaths(new ApiError(404, "not found", null)).size).toBe(0);
    expect(invalidFieldPaths(new Error("boom")).size).toBe(0);
  });
});

describe("resilience — this code runs inside catch blocks", () => {
  /**
   * A formatter that throws replaces a *handled* failure with an unhandled
   * one, which is strictly worse than the generic message it exists to
   * improve on. These cases all reached `describeApiError` in practice: a
   * partial ``vi.mock("@/api/caliberApi", …)`` factory leaves ``ApiError``
   * undefined, so a plain `instanceof` check raises a TypeError before any
   * error can be rendered.
   */
  const HOSTILE_INPUTS: Array<[string, unknown]> = [
    ["null", null],
    ["undefined", undefined],
    ["a thrown string", "boom"],
    ["a thrown number", 500],
    ["a bare object", {}],
    ["an object with a hostile getter", {
      get message() {
        throw new Error("getter exploded");
      },
    }],
    ["an ApiError-shaped object with a non-array errors field", {
      name: "ApiError",
      status: 400,
      message: "request body validation failed",
      body: { detail: "d", status_code: 400, errors: "not-an-array" },
    }],
    ["an ApiError-shaped object with a null body", {
      name: "ApiError",
      status: 400,
      message: "failed",
      body: null,
    }],
  ];

  it.each(HOSTILE_INPUTS)("describeApiError survives %s", (_label, input) => {
    expect(() => describeApiError(input)).not.toThrow();
    const described = describeApiError(input);
    expect(typeof described.summary).toBe("string");
    expect(described.summary.length).toBeGreaterThan(0);
    expect(Array.isArray(described.issues)).toBe(true);
  });

  it.each(HOSTILE_INPUTS)("apiErrorText survives %s", (_label, input) => {
    expect(() => apiErrorText(input, "fallback")).not.toThrow();
    expect(typeof apiErrorText(input, "fallback")).toBe("string");
  });

  it("recognises a structurally-ApiError error whose class identity is lost", () => {
    // What a partially-mocked module (or a duplicated module instance)
    // produces: the right shape, but failing `instanceof`.
    const detached = {
      name: "ApiError",
      status: 400,
      message: "request body validation failed",
      body: {
        detail: "request body validation failed",
        status_code: 400,
        errors: [{ loc: ["name"], msg: "field required", type: "missing" }],
      },
    };

    const described = describeApiError(detached);
    expect(described.status).toBe(400);
    expect(described.issues).toHaveLength(1);
    expect(described.issues[0].label).toBe("Name");
  });

  it("does not mistake an ordinary Error for an ApiError", () => {
    const described = describeApiError(new Error("Failed to fetch"));
    expect(described.status).toBeNull();
    expect(described.issues).toEqual([]);
  });
});

