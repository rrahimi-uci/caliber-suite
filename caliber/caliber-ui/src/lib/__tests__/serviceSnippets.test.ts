import { describe, expect, it } from "vitest";

import {
  buildServiceSnippet,
  sampleInput,
  SNIPPET_LANGUAGES,
} from "@/lib/serviceSnippets";

const ENDPOINT = "https://app.example.com/ajax-api/2.0/mlflow/caliber/services/WF-1/invoke";
const STATUS_BASE = "https://app.example.com/ajax-api/2.0/mlflow/caliber/services/WF-1/runs";

const SCHEMA = {
  type: "object",
  properties: {
    query: { type: "string" },
    top_k: { type: "integer" },
    strict: { type: "boolean" },
  },
};

describe("sampleInput", () => {
  it("builds a typed placeholder object from a JSON schema", () => {
    expect(sampleInput(SCHEMA)).toEqual({ query: "...", top_k: 0, strict: false });
  });

  it("returns an empty object for an empty or non-object schema", () => {
    expect(sampleInput({})).toEqual({});
    expect(sampleInput(undefined)).toEqual({});
    expect(sampleInput({ type: "string" })).toEqual({});
  });
});

describe("buildServiceSnippet", () => {
  it("exposes curl, Python, and JavaScript", () => {
    expect(SNIPPET_LANGUAGES.map((l) => l.id)).toEqual(["curl", "python", "javascript"]);
  });

  it("curl: posts to the endpoint and shows the poll step", () => {
    const snip = buildServiceSnippet({
      endpoint: ENDPOINT,
      inputSchema: SCHEMA,
      authRequired: false,
      language: "curl",
    });
    expect(snip).toContain(`curl -X POST '${ENDPOINT}'`);
    expect(snip).toContain('"input":');
    expect(snip).toContain(`${STATUS_BASE}/RUN_ID`);
    // No auth header when the service is open.
    expect(snip).not.toContain("Authorization");
  });

  it("curl: includes a Bearer header on both calls when auth is required", () => {
    const snip = buildServiceSnippet({
      endpoint: ENDPOINT,
      inputSchema: SCHEMA,
      authRequired: true,
      language: "curl",
    });
    expect(snip.match(/Authorization: Bearer YOUR_TOKEN/g)?.length).toBe(2);
  });

  it("python: emits valid-looking requests code with a poll loop and Python literals", () => {
    const snip = buildServiceSnippet({
      endpoint: ENDPOINT,
      inputSchema: SCHEMA,
      authRequired: true,
      language: "python",
    });
    expect(snip).toContain("import requests");
    expect(snip).toContain('HEADERS = {"Authorization": "Bearer YOUR_TOKEN"}');
    expect(snip).toContain("requests.post(BASE, json=payload, headers=HEADERS)");
    expect(snip).toContain('run_id = ack["run_id"]');
    // JSON `false` must be rendered as a Python literal.
    expect(snip).toContain('"strict": False');
    expect(snip).not.toContain("false");
  });

  it("javascript: emits fetch code with a terminal-status poll loop", () => {
    const snip = buildServiceSnippet({
      endpoint: ENDPOINT,
      inputSchema: SCHEMA,
      authRequired: false,
      language: "javascript",
    });
    expect(snip).toContain("await fetch(BASE");
    expect(snip).toContain('const terminal = ["completed","failed","cancelled","expired"]');
    expect(snip).toContain("setTimeout");
    expect(snip).not.toContain("YOUR_TOKEN");
  });
});
