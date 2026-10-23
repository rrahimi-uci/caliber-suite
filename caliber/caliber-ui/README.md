# CALIBER web app

The single-page application served by the standalone CALIBER backend at `/caliber/`. For the
backend, how to install and run it, see [`../README.md`](../README.md).

## Overview

This app is the browser control plane for CALIBER: sign in, work in a workspace (the API calls them
projects), and author, evaluate, release and roll back prompts, skills, tools and workflows. The sidebar
groups these pages: Dashboard, Prompts, Test Sets, Evaluations, Releases, Skills, Tools, Workflows,
Knowledge Bases, Object Store, Judges, Agents, Observability, Review Queues, Audit Log, LLM Gateway,
Administration and Settings. Four areas sit behind server-side feature flags and are hidden while the flag
is off: Plans and the Aria assistant (`CALIBER_FEATURE_ARIA`, on by default), MCP Servers and OpenAPI
Integrations (`CALIBER_FEATURE_INTEGRATIONS`), Cookbooks (`CALIBER_FEATURE_COOKBOOKS`), and the
release-candidate part of Releases (`CALIBER_FEATURE_RELEASE_CANDIDATES`). See
[Alpha scope](../../docs/start/alpha-scope.md#feature-flags).

In development it runs on Vite. In production the standalone Python backend serves the built bundle at
`/caliber/`.

## Stack

- **Vite 8**, **React 18** and **TypeScript** in strict mode.
- **Tailwind CSS 3** plus Radix UI primitives for accessible controls.
- **React Router 7** for client-side routing under `/caliber/`, and **@xyflow/react** for the workflow canvas.
- **@tanstack/react-query** for cached API state, with a legacy `useApi` hook still used by older pages.
- Native **`EventSource`** for the live-update channel (`GET /events/stream`).
- **Vitest**, Testing Library and MSW for unit and integration tests, and **Playwright** for browser tests.

## Quick start

You need Node.js 20.19 or later (or 22.12 or later) with npm, and a running CALIBER backend
(see [`../README.md`](../README.md#run)).

```bash
cd caliber/caliber-ui
npm install          # one time (use `npm ci` for a clean, lockfile-exact install, as CI does)
npm run dev          # Vite dev server with hot reload
```

The app is then at `http://localhost:5173/caliber/`. The dev server proxies `/ajax-api/*` to the backend at
`http://localhost:5001` (change it with `CALIBER_API_TARGET`), so start the backend first and sign in with the
account you created there. After you sign in, the Dashboard page loads `GET /dashboard/summary` and subscribes
to `GET /events/stream` for live updates.

`npm run dev` and `npm run build` first run `scripts/sync-docs.mjs`, which regenerates the documentation
site from the Markdown sources under `docs/` and copies it into `public/docs/` (served in the app at
`/caliber/docs/`). Those copies are generated: edit the Markdown sources, not the output.

## Configuration

| Env var | Where | Default | Notes |
| --- | --- | --- | --- |
| `CALIBER_API_TARGET` | Vite dev server | `http://localhost:5001` | Backend URL the dev proxy forwards `/ajax-api/*` to. |
| `CALIBER_UI_BASE` | Vite build | `/caliber/` | Public base path of the built app. The backend serves the SPA at `/caliber/`, so keep the default unless a reverse proxy mounts it elsewhere. |

At runtime the backend stamps `window.__CALIBER_STATIC_PREFIX__` into the served `index.html`, from the
backend's `CALIBER_STATIC_PREFIX` setting, so the API client and the router agree on a reverse-proxy mount
point. It defaults to an empty string (no prefix).

## Scripts

Run these from `caliber/caliber-ui/`.

| Command | What it does |
| --- | --- |
| `npm run dev` | Vite dev server with hot reload and the backend proxy. |
| `npm run typecheck` | `tsc --noEmit -p tsconfig.app.json`: strict type check of the app code. |
| `npm run lint` | ESLint over the tree. |
| `npm run format` | Prettier, write in place. |
| `npm test` | Vitest unit and integration suite, once. |
| `npm run test:coverage` | The same suite with V8 coverage; this is the CI run. |
| `npm run build` | Sync the docs, type-check, then build to `dist/`. |
| `npm run preview` | Serve `dist/` locally to sanity-check the production build. |
| `npm run playwright:install` | Install Chromium for Playwright (once). |
| `npm run test:e2e:alpha` | The alpha journey browser tests, on an isolated server with the shipped feature-flag defaults. |
| `npm run test:e2e:cookbooks` | The cookbook browser journeys, on an isolated server with the cookbooks flag on. |
| `npm run test:e2e` | The full Playwright suite, on an isolated server with every feature flag on. |
| `npm run test:e2e:docs` | The docs-shell specs, against a backend you already started (see below). |
| `npm run test:e2e:age` | The `@age` knowledge-base specs, against a backend you already started on PostgreSQL with Apache AGE. |
| `npm run test:e2e:headed`, `npm run test:e2e:debug` | The full suite with a visible browser, or in the Playwright debugger. |

There are more targeted scripts in `package.json` (`test:e2e:workflow-platform`, the Allure report scripts,
and `ux:census` / `ux:check-ledger`, which are maintainer tools).

## Testing

**Unit and integration.** `npm run test:coverage` is what CI runs. Vitest runs on one worker, on purpose:
the heavy jsdom suites time out when parallel workers contend. Its coverage thresholds
(lines 89, statements 87, functions 87, branches 78) are in `vitest.config.ts`; Playwright coverage is not
part of that report. CI also type-checks, lints and builds the app, and requires the generated docs to match
their sources.

**Browser tests.** One-time setup: `npm run playwright:install`. The Playwright configs start their own
disposable CALIBER server (by default a fresh SQLite database under `caliber/.tmp/`, removed afterwards) through
`../scripts/run-playwright-server.sh`, which needs the backend virtual environment, `caliber/.venv`: run
`make install` in `caliber/` first. The first start can take several minutes. `npm run test:e2e` also reads
`CALIBER_DATABASE_URL`, `POSTGRES_URL` and the two `CALIBER_KNOWLEDGE_AGE_*` settings from a repository-root `.env`, so a
root `.env` that points at PostgreSQL makes it use that database; the alpha and cookbook configs ignore the file.

| Config | Command | Port | What it covers |
| --- | --- | --- | --- |
| `playwright.alpha.config.ts` | `npm run test:e2e:alpha` | 5170 (`CALIBER_ALPHA_E2E_PORT`) | The alpha journeys with the shipped flag defaults. CI runs this. |
| `playwright.cookbooks.config.ts` | `npm run test:e2e:cookbooks` | 5160 (`CALIBER_COOKBOOK_E2E_PORT`) | The cookbook journeys, driven only through the UI. CI runs this. |
| `playwright.config.ts` | `npm run test:e2e` | 5150 (`MLFLOW_PORT`) | Every spec except the flag-default and per-lab specs, with all four feature flags on. Some specs expect the Docker stack's MinIO. |
| `playwright.labs.config.ts` | `npx playwright test --config=playwright.labs.config.ts` | your server | One spec per cookbook lab, against a server you start with `scripts/cookbook-env.sh`; see [`e2e/cookbooks/labs/README.md`](e2e/cookbooks/labs/README.md). |

To run specs against a backend you already started, set `CALIBER_E2E_USE_EXISTING_SERVER=1` and
`CALIBER_E2E_BASE_URL` (default `http://127.0.0.1:5150/caliber`). `test:e2e:docs` and `test:e2e:age` do this for
`http://127.0.0.1:5001/caliber`. Such a server must be started with the feature flags the specs need
(all four on, for `npm run test:e2e`). Set `PLAYWRIGHT_WORKERS` to change the default of four workers.

## Project layout

```text
caliber-ui/
├── e2e/                    Playwright specs (and e2e/cookbooks/ for the cookbook journeys and lab specs)
├── scripts/                Node tooling: docs sync, Allure reports, UX census
├── public/                 Static assets; public/docs/ is the generated documentation site
└── src/
    ├── api/                caliberApi.ts (fetch wrapper and typed endpoint functions) and response types
    ├── auth/               Local sign-in state
    ├── components/         AppShell, Sidebar, TopBar, shared UI, and feature folders (aria/, assistant/,
    │                       workflows/, knowledge/, objectstore/, observability/, tools/, ...)
    ├── hooks/              useApi, useApiQuery, useDashboardSummary, useEventStream, useFeature, ...
    ├── lib/                Pure helpers
    ├── pages/              One file per route (Overview.tsx is the Dashboard; Prompts.tsx, Settings.tsx, ...)
    ├── styles/             Tailwind directives and component utilities
    ├── test/               MSW handlers and the jsdom test setup
    ├── workspace/          The active-workspace (project) selection
    ├── App.tsx             Router and page chrome
    └── main.tsx            Entry point and the router base path
```

## How the app is served in production

The backend serves the built bundle from `caliber.routes.static`. The Python wheel includes the packaged
assets from `src/caliber/ui`; to build them from a checkout, run `make ui` in `caliber/` (or let CI or release
automation build `caliber-ui/dist` and copy it there before it builds the wheel). If the bundle is absent,
the backend answers `/caliber/` with an operator-facing 503 page that explains how to build it or start this
dev server; the API keeps working.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `npm install` or `npm run dev` fails on syntax or engine errors | Use Node.js 20.19 or later (or 22.12 or later). |
| The browser is missing for Playwright | Run `npm run playwright:install`. |
| Playwright says the virtualenv was not found | Run `make install` in `caliber/` so `caliber/.venv` exists. |
| API calls hit the wrong backend, or fail with 502 | Start the backend, and set `CALIBER_API_TARGET` before `npm run dev` if it is not on `http://localhost:5001`. |
| Routes render 404 in production | Serve the app under the same base path it was built with (`CALIBER_UI_BASE`, default `/caliber/`). |
| A page you expect is missing from the sidebar | Its feature flag is off; see the flags above. |
| Test coverage is unexpectedly low | Run `npm run test:coverage`. Playwright coverage is not part of Vitest's V8 report. |
