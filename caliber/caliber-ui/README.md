# CALIBER UI

Single-page application served by the standalone CALIBER backend.

## Overview

This app is the browser control plane for CALIBER. It covers login, overview,
prompts, tools, skills, MCP servers, file directories, workflows, workflow runs,
approvals, settings, and assistant authoring. In development it runs on Vite; in
production the standalone Python backend serves the built bundle at `/caliber/`.

## Stack

- **Vite 8** + **React 18** + **TypeScript** in strict mode.
- **Tailwind CSS** plus Radix UI primitives for accessible controls.
- **React Router** for client-side routing under `/caliber/`.
- **@tanstack/react-query** for cached API state, with a legacy `useApi` hook still used by older pages.
- Native **`EventSource`** for the SSE live-update channel.
- **Vitest**, Testing Library, MSW, and **Playwright** for unit/integration/E2E tests.

## Quick start

```bash
# Install dependencies (one-time)
npm install

# Run the dev server with HMR.
# Vite proxies /ajax-api/* to http://localhost:5001 by default — start
# standalone CALIBER separately:
#   (in caliber/) uvicorn caliber.server:create_app --factory --reload --port 5001
npm run dev
```

The SPA is served from `http://localhost:5173/caliber/` in dev. Open that URL — the Overview page hits `GET /dashboard/summary` and subscribes to `GET /events/stream` for live updates.

## Configuration

| Env var              | Where      | Default                 | Notes                                                                             |
| -------------------- | ---------- | ----------------------- | --------------------------------------------------------------------------------- |
| `CALIBER_API_TARGET` | Vite dev   | `http://localhost:5001` | Backend URL the dev proxy forwards `/ajax-api/*` to.                              |
| `CALIBER_UI_BASE`    | Vite build | `/caliber/`             | Public base path of the built SPA. Override for non-default reverse-proxy mounts. |

At runtime the hosting backend stamps `window.__CALIBER_STATIC_PREFIX__` into the served `index.html` (e.g. `"/mlflow"`) so the API client and the router agree on the deployment's mount point. Locally the value defaults to `""` (no prefix).

## Scripts

| Command                      | What it does                                                                                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm run dev`                | Vite dev server with HMR + backend proxy.                                                                                                      |
| `npm run typecheck`          | `tsc --noEmit -p tsconfig.app.json` (strict app-code typecheck; excludes test fixtures).                                                       |
| `npm run build`              | Regenerate/sync docs, type-check, then build to `dist/`. CI explicitly stages `dist/` into `src/caliber/ui` before Hatchling builds the wheel. |
| `npm run preview`            | Serve `dist/` locally to sanity-check the production build.                                                                                    |
| `npm run lint`               | ESLint over the UI tree.                                                                                                                       |
| `npm run format`             | Prettier write-in-place over the tree.                                                                                                         |
| `npm run test:coverage`      | Vitest unit/integration suite with V8 coverage.                                                                                                |
| `npm run test:e2e`           | Playwright E2E suite.                                                                                                                          |
| `npm run playwright:install` | Install Chromium for Playwright.                                                                                                               |

## Project layout

```
caliber-ui/
├── e2e/                    Playwright tests
└── src/
    ├── api/
    │   ├── caliberApi.ts       Thin fetch wrapper + typed endpoint functions
    │   ├── types.ts            Response shapes mirroring backend schemas
    │   └── workflowTypes.ts    Workflow-specific response and request types
    ├── components/
    │   ├── AppShell.tsx        Top bar + sidebar + main content slot
    │   ├── assistant/          Assistant panel and draft/test subcomponents
    │   ├── workflows/          Canvas, node details, run file panels
    │   ├── Sidebar.tsx         Left navigation with prefix-matched active states
    │   ├── StatCard.tsx        Dashboard stat card (link-or-static)
    │   └── TopBar.tsx          Fixed top header
    ├── hooks/
    │   ├── useApi.ts           Minimal data-fetching hook (data/error/loading/refresh)
    │   └── useEventStream.ts   EventSource subscription with type filtering
    ├── pages/
    │   ├── Overview.tsx        Overview / Dashboard landing page
    │   ├── Settings.tsx        Admin settings inventory
    │   ├── WorkflowDetail.tsx  Workflow graph, versions, deployments, runs
    │   └── WorkflowEditor.tsx  Visual workflow builder
    ├── styles/
    │   └── index.css           Tailwind directives + component utilities
    ├── App.tsx                 Router + chrome
    └── main.tsx                Entry point + BrowserRouter basename
```

## How the SPA is served in production

The CALIBER ASGI backend serves the built bundle from `caliber.routes.static`, whether it
is mounted as an MLflow app or started as a standalone service. The
Python wheel includes packaged SPA assets from `src/caliber/ui`; CI or release
automation should build `caliber-ui/dist` and copy it there before producing the
wheel. If the bundle is absent, the backend returns an operator-facing 503 with
instructions to start the SPA dev server or rebuild the package.

## Current validation

CI type-checks, runs Vitest, builds the production bundle, enforces generated-doc
parity, and publishes test evidence when account-level artifact storage is
available. Playwright is part of `test-all.sh` / `make allure-report`, not the UI
unit-test job. See the suite-level
[`product-complete-report.md`](../../product-complete-report.md) for dated evidence
and residual limitations; historical totals are not a current pass claim.

## Troubleshooting

| Symptom                           | Check                                                                                   |
| --------------------------------- | --------------------------------------------------------------------------------------- |
| E2E browser is missing            | Run `npm run playwright:install`.                                                       |
| API calls hit the wrong backend   | Set `CALIBER_API_TARGET` before `npm run dev`.                                          |
| Routes render 404 in production   | Verify the app is served under the same base path as `CALIBER_UI_BASE`.                 |
| Test coverage is unexpectedly low | Run `npm run test:coverage`; Playwright coverage is not included in Vitest's V8 report. |
