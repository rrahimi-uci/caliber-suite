import { describe, expect, it } from "vitest";

import {
  ALL_SCOPES,
  SCOPE_ADMIN,
  SCOPE_APPROVER,
  SCOPE_OPERATOR,
  SCOPE_VIEWER,
  accessLevel,
  canAnyScope,
  hasScope,
  missingScopeReason,
  scopeLabel,
} from "@/lib/scopes";

/**
 * The effective scope sets ``GET /me`` actually returns. ``scopes_for_user``
 * (caliber/src/caliber/auth.py) expands ``_SCOPE_IMPLIES`` *before* responding,
 * so these are the real payloads — not a client-side reconstruction.
 */
const ME_ADMIN = [SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER];
const ME_APPROVER = [SCOPE_APPROVER, SCOPE_VIEWER];
const ME_OPERATOR = [SCOPE_OPERATOR, SCOPE_VIEWER];
const ME_VIEWER = [SCOPE_VIEWER];
const ME_ANONYMOUS: string[] = [];

describe("scope constants", () => {
  it("uses the prefixed names the server issues, never the bare ones", () => {
    // This is the regression the whole module exists for: matching "admin"
    // against a payload of "caliber.admin" fails closed and silently.
    expect(SCOPE_VIEWER).toBe("caliber.viewer");
    expect(SCOPE_OPERATOR).toBe("caliber.operator");
    expect(SCOPE_APPROVER).toBe("caliber.approver");
    expect(SCOPE_ADMIN).toBe("caliber.admin");
    for (const scope of ALL_SCOPES) {
      expect(scope.startsWith("caliber.")).toBe(true);
    }
  });

  it("orders ALL_SCOPES most-privileged first", () => {
    expect([...ALL_SCOPES]).toEqual([
      SCOPE_ADMIN,
      SCOPE_APPROVER,
      SCOPE_OPERATOR,
      SCOPE_VIEWER,
    ]);
  });
});

describe("hasScope", () => {
  it("matches a scope present in the effective set", () => {
    expect(hasScope(ME_OPERATOR, SCOPE_OPERATOR)).toBe(true);
    expect(hasScope(ME_ADMIN, SCOPE_OPERATOR)).toBe(true);
  });

  it("does not match a scope absent from the effective set", () => {
    expect(hasScope(ME_OPERATOR, SCOPE_ADMIN)).toBe(false);
    expect(hasScope(ME_VIEWER, SCOPE_OPERATOR)).toBe(false);
  });

  it("relies on the server's expansion rather than re-implementing it", () => {
    // An admin payload carries operator explicitly, so membership suffices.
    expect(hasScope(ME_ADMIN, SCOPE_VIEWER)).toBe(true);
    // And a hypothetical unexpanded payload is NOT silently widened here —
    // duplicating _SCOPE_IMPLIES client-side is exactly what we refuse to do.
    expect(hasScope([SCOPE_ADMIN], SCOPE_OPERATOR)).toBe(false);
  });

  it("treats a not-yet-loaded identity as no authority", () => {
    expect(hasScope(undefined, SCOPE_VIEWER)).toBe(false);
    expect(hasScope(null, SCOPE_VIEWER)).toBe(false);
    expect(hasScope(ME_ANONYMOUS, SCOPE_VIEWER)).toBe(false);
  });

  it("does not match on a bare or partial scope name", () => {
    // Guards against a substring-style implementation: "operator" must not
    // satisfy a check for "caliber.operator", and vice versa.
    expect(hasScope(["operator"], SCOPE_OPERATOR)).toBe(false);
    expect(hasScope(["admin"], SCOPE_ADMIN)).toBe(false);
    expect(hasScope(["caliber.operator.readonly"], SCOPE_OPERATOR)).toBe(false);
  });
});

describe("canAnyScope", () => {
  it("passes when any one of the required scopes is held", () => {
    expect(canAnyScope(ME_APPROVER, [SCOPE_ADMIN, SCOPE_APPROVER])).toBe(true);
    expect(canAnyScope(ME_ADMIN, [SCOPE_ADMIN])).toBe(true);
  });

  it("fails when none of the required scopes is held", () => {
    expect(canAnyScope(ME_VIEWER, [SCOPE_ADMIN, SCOPE_OPERATOR])).toBe(false);
  });

  it("fails closed on an empty requirement list", () => {
    // Mirrors require_scopes' defensive stance: a call site that never said
    // what it needs must not read as "needs nothing".
    expect(canAnyScope(ME_ADMIN, [])).toBe(false);
  });

  it("fails closed while identity is loading", () => {
    expect(canAnyScope(undefined, [SCOPE_VIEWER])).toBe(false);
  });
});

describe("accessLevel", () => {
  it("labels an admin Admin — the reported bug", () => {
    // Before the fix this returned "Viewer" for every admin, because the badge
    // compared against the bare "admin".
    expect(accessLevel(ME_ADMIN).label).toBe("Admin");
    expect(accessLevel(ME_ADMIN).scope).toBe(SCOPE_ADMIN);
  });

  it("labels an approver Approver rather than collapsing them to Viewer", () => {
    expect(accessLevel(ME_APPROVER).label).toBe("Approver");
    expect(accessLevel(ME_APPROVER).scope).toBe(SCOPE_APPROVER);
  });

  it("labels operator and viewer correctly", () => {
    expect(accessLevel(ME_OPERATOR).label).toBe("Operator");
    expect(accessLevel(ME_VIEWER).label).toBe("Viewer");
  });

  it("reports the highest scope held, not the first listed", () => {
    // Order in the payload must not decide the label.
    expect(accessLevel([SCOPE_VIEWER, SCOPE_OPERATOR, SCOPE_ADMIN]).label).toBe(
      "Admin",
    );
    expect(accessLevel([...ME_ADMIN].reverse()).label).toBe("Admin");
  });

  it("names the anonymous state instead of implying read access", () => {
    const level = accessLevel(ME_ANONYMOUS);
    expect(level.label).toBe("No access");
    expect(level.scope).toBeNull();
    expect(accessLevel(undefined).label).toBe("No access");
  });

  it("carries a non-empty summary for every level", () => {
    for (const scopes of [ME_ADMIN, ME_APPROVER, ME_OPERATOR, ME_VIEWER, ME_ANONYMOUS]) {
      expect(accessLevel(scopes).summary.length).toBeGreaterThan(0);
    }
  });
});

describe("scopeLabel / missingScopeReason", () => {
  it("names each scope in human terms", () => {
    expect(scopeLabel(SCOPE_ADMIN)).toBe("Admin");
    expect(scopeLabel(SCOPE_APPROVER)).toBe("Approver");
  });

  it("states a single requirement plainly", () => {
    expect(missingScopeReason([SCOPE_OPERATOR])).toBe("Requires Operator access.");
  });

  it("joins multiple requirements with 'or'", () => {
    expect(missingScopeReason([SCOPE_OPERATOR, SCOPE_ADMIN])).toBe(
      "Requires Operator or Admin access.",
    );
    expect(missingScopeReason([SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR])).toBe(
      "Requires Admin, Approver or Operator access.",
    );
  });

  it("degrades safely with no requirement", () => {
    expect(missingScopeReason([])).toBe("Not available.");
  });
});
