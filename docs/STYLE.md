# CALIBER documentation style guide

This is the contract every module doc under `docs/NN-name/*.md` follows, plus the
diagram conventions the generator (`docs-site/build-docs.mjs`) and the shared
client (`docs-site/docs.js`) understand. It exists so the docs read consistently
top-down — a newcomer gets oriented before any deep reference — and so diagrams
look like one designed system rather than sixteen unrelated sketches.

Operate at the **markdown + generator** layer, never the generated HTML in
`docs-site/m-*.html` — that is overwritten on every build. One generator change
propagates to all pages at once; that is the leverage.

## The page contract

Each `docs/NN-name/architecture.md` follows this shape, top to bottom:

1. `# H1` — the page title.
2. **"What this is"** — one short paragraph in plain language. No code-path names,
   no table names; just what the thing is and why it exists.
3. **`## At a glance`** — a 5–7 row, two-column orientation table ("where it
   stands"). Mandatory. Every fact must be supported elsewhere in the doc.
4. A **signature diagram** — themed, with semantic node colors and a legend.
5. **`## Reference`** — the explicit tier boundary. The generator renders this as
   a banded "Deep reference" section; everything after it is the detailed tier.
6. Below the boundary: the deep sections (scope, module boundaries, data model,
   API surface, lifecycle, security, observability, extension points). The
   generator wraps each `##` section here in a collapsible, default-open panel.

The break between tier 1 (Overview) and tier 2 (Reference) is driven by **reader
intent**, not topic: a newcomer lives above `## Reference`; a builder jumps below
it. Keep the Overview genuinely conceptual — it is not a condensed reference.

## Diagram conventions

Diagrams are usually Mermaid fenced blocks (` ```mermaid `). The shared client
themes them to the CALIBER violet palette automatically (light + dark) — never
hand-set colors in the diagram source.

For the handful of pages that need a presentation-grade signature diagram, use a
`diagram-svg` fence that points at a checked-in SVG asset relative to the doc:

````
```diagram-svg
assets/platform-overview.svg
```
````

The generator inlines that SVG into the page, so the asset can use the theme
variables from `docs.css`. Treat this as the exception path for hero diagrams,
not the default format for everyday reference diagrams.

### Semantic node colors (the typed-color system)

Tag each node with a role class using Mermaid's `:::class` syntax. The client
supplies the (theme-aware) `classDef`s; you only tag. Use one class per role,
consistently, on every page:

| Class | Role | Examples |
| --- | --- | --- |
| `:::user` | People / external actors entering the system | Browser, User, Operator |
| `:::ui` | Frontend / SPA surfaces | React SPA, Workflow Studio, page modules |
| `:::ctrl` | CALIBER control plane (routes, runtime, services) | Route modules, interpreter, services |
| `:::store` | Durable storage | SQLAlchemy DB, object store, pgvector |
| `:::ext` | External systems CALIBER integrates with | MLflow, LLM providers, MCP servers, Gateway |
| `:::async` | Off-request asynchronous execution | Background workers, queues, the event bus |

Example:

```
flowchart LR
    B[Browser]:::user --> SPA[React SPA]:::ui
    SPA --> API[Route modules]:::ctrl
    API --> DB[(Metadata DB)]:::store
    API --> ML[MLflow]:::ext
    WK[Background workers]:::async --> DB
```

After a Mermaid signature diagram, add a legend with an empty fenced `legend`
block — the generator renders the full color key:

````
```legend
```
````

### Diagram hygiene

- One idea per diagram. Prefer `flowchart LR` for topology, `sequenceDiagram` for
  request/response flows.
- In a `sequenceDiagram` message, never use `;` (Mermaid reads it as a statement
  separator) — use `,` or `and`.
- For a line break inside a node label, write a literal `\n`; the generator
  converts it to `<br/>` and the client restores it at render time.
- A `diagram-svg` asset must be static SVG only: no scripts, no event handlers.
- Keep SVG assets alongside the doc that uses them, under a local `assets/`
  folder, and include the legend inside the SVG so the diagram is self-contained.

## Tables

Use GFM tables for any "key → meaning" reference (data model, API surface,
config). The first column is the key; the generator weights it automatically.
Keep right-column cells to one tight phrase. Wrap identifiers, paths, and
endpoints in `inline code`.

## Voice

Precise, declarative, vendor-neutral. State constraints plainly rather than
hiding them. Match the existing architecture-series tone.
