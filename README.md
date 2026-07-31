<div align="center">

<img src="docs-site/caliber.png" alt="CALIBER" width="200"/>

# CALIBER Suite

### The MLflow-integrated control plane for trusted agentic workflows

**Design, evaluate, calibrate, deploy, and observe AI-agent resources with
asset-specific governance** — prompts, tools, skills, MCP servers, workflows,
and knowledge bases —
**in one place, on infrastructure you already run.**

[![CI](https://github.com/rrahimi-uci/caliber-suite/actions/workflows/ci.yml/badge.svg)](https://github.com/rrahimi-uci/caliber-suite/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./caliber/LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%20–%203.12-3776AB?logo=python&logoColor=white)](./caliber/README.md)
[![MLflow](https://img.shields.io/badge/MLflow-≥3.14-0194E2?logo=mlflow&logoColor=white)](https://mlflow.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![React](https://img.shields.io/badge/React-SPA-61DAFB?logo=react&logoColor=white)](./caliber/caliber-ui)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./caliber/CONTRIBUTING.md)

**[Quickstart](#-quickstart)** · **[The loop](#-the-refinement-loop)** · **[Architecture](#-architecture)** · **[Capabilities](#-whats-inside)** · **[Docs](#-documentation)** · **[Status](#-project-status)**

</div>

---

CALIBER is an **MLflow-integrated ASGI control plane** and **React application** for the lifecycle of AI-agent
resources. It closes the loop that most observability tools leave open: catching a production failure is
well-tooled — *doing something about it, at scale, with a human in the loop and an audit trail* is not.
CALIBER's canonical prompt-refinement path turns a flagged trace into a measured candidate that a human
explicitly reviews before any alias update. Evaluation gates govern refinement jobs and travel as advisory
evidence; audited rollback exists only where an asset records live-target or snapshot history, and its
semantics remain asset-specific. The implementation reuses MLflow's
Experiments, Traces, Assessments, Prompt Registry, Artifact Store, and `genai.evaluate`. It supports an
in-process `mlflow.app` plugin topology and a standalone CALIBER service that talks to MLflow over HTTP.

> 📖 **Live docs:** [`docs-site/index.html`](./docs-site/index.html) &nbsp;·&nbsp; 📦 **Source-only distribution metadata:** `caliber-suite` (`caliber` import/app) &nbsp;·&nbsp; 🧑‍🍳 **Learn by doing:** [16 Cookbooks](./docs-site/m-16-cookbooks.html)

---

## 🔄 The refinement loop

The canonical refinement path takes a thumbs-down in production to a measured, reviewed candidate with
**two human decisions**: verification and an explicit operator Apply action. Runtime depends on the provider, dataset, and optimizer;
the repository does not claim a universal time-to-fix.

```mermaid
flowchart LR
    trace["Production agent<br/>MLflow trace"]:::src
    flag["Response flagged<br/>MLflow Assessment"]:::src
    v["① Verify<br/>human · 1 click"]:::human
    d["② Diagnose<br/>LLM root-cause"]:::auto
    g["③ Generate<br/>policy-selected optimizer"]:::auto
    e["④ Evaluate<br/>EvalProvider + regression gate"]:::auto
    a["⑤ Apply<br/>operator · diff + eval"]:::human
    p["⑥ Promote<br/>audited alias rotation"]:::ship

    trace --> flag --> v --> d --> g --> e --> a --> p
    p -.->|"agent loads @prod — no code change"| trace

    classDef src fill:#f1f5f9,stroke:#64748b,color:#0f172a;
    classDef human fill:#fef3c7,stroke:#d97706,color:#78350f;
    classDef auto fill:#dbeafe,stroke:#2563eb,color:#1e3a8f;
    classDef ship fill:#dcfce7,stroke:#16a34a,color:#14532d;
```

<div align="center"><sub>🟡 human decision &nbsp;·&nbsp; 🔵 automated &nbsp;·&nbsp; 🟢 shipped — the dashed edge closes the loop: your code keeps loading <code>@prod</code>, the next call gets the new version.</sub></div>

**Why it matters** — changes reuse MLflow evidence, carry per-dimension evaluation and an advisory gate
verdict into review, and retain an audit trail. Promotion authorization remains explicit; a gate verdict
is evidence, not an unbypassable cross-artifact policy. Automatic multi-agent bundle optimization remains
roadmap work rather than a shipped optimizer.

---

## 🧩 What's inside

A single control plane spanning the whole agent stack. Implementation and verification status varies by
surface; the repository audit records the exact current boundary.

| Area | Highlights |
| --- | --- |
| ✍️ **Authoring** | **Prompts**, **Tools**, **Skills**, **MCP servers** — governed definitions with asset-specific testing and governance; versioning, gates, live aliases, and rollback apply only where each asset implements them |
| 🔀 **Workflows** | Visual **Studio**, queued runs, runtime approvals, checkpointing, and workflow-as-a-service |
| 📚 **Knowledge bases** | Versioned RAG corpora — chunking, embeddings, **Apache AGE** graph extraction, hybrid retrieval + cross-encoder rerank |
| 🧪 **Evaluation** | Scorecards, **custom LLM judges** (`make_judge`), structured human-review queues, and path-specific advisory gate verdicts |
| 🎛️ **Calibration** | Five implemented provider paths: the prompt form exposes MetaPrompt and GEPA; policy can select SkillMetaPrompt and DSPy BootstrapFewShot; DSPy MIPRO requires explicit configuration |
| 🔭 **Observability** | MLflow tracing with multimodal attachments, SSE live events, service visibility, and trace retention |
| 🛡️ **Governance** | RBAC, path-specific audited promotion/rollback, and the **LLM Gateway** surface (endpoint discovery, guardrails, per-model pricing, usage) |
| 🤖 **Aria copilot** | An embedded, permissioned **agentic tool loop** (OpenAI + Claude) with durable goal-plans, async resume, and self-correction; plan gates support permission prompts and separation of duties where an operation is routed through a gated step |

---

## 🏢 Architecture

> 🏛️ **For the layered, executive-and-architect view — the six-layer stack, the canonical chain, the
> anatomy of a governed asset, and the per-asset guarantees — read
> [`ARCHITECTURE.md`](./ARCHITECTURE.md).** The section below is the deployment-topology summary.

The same CALIBER ASGI application supports two deployment topologies. Native development can mount it as
an in-process MLflow `mlflow.app`; the bundled loopback Compose stack runs CALIBER as a standalone service
and connects to a vanilla MLflow server over HTTP. `CALIBER_DATABASE_URL` is independent of MLflow's backend
store; the bundled stack deliberately uses separate `caliber` and `mlflow` databases to avoid migration
ownership collisions.

```mermaid
flowchart TB
    spa["React SPA — /caliber/"]:::ui

    subgraph embedded["Topology A · embedded mlflow.app"]
        eproc["MLflow server process<br/>MLflow core + CALIBER ASGI"]:::plug
    end

    subgraph standalone["Topology B · bundled standalone service"]
        capi["CALIBER ASGI :5001<br/>API · SPA · in-process workers"]:::plug
        mflow["MLflow :5000<br/>traces · registry · evaluate"]:::core
        capi -->|"MLFLOW_TRACKING_URI / HTTP"| mflow
    end

    cdb[("CALIBER metadata DB<br/>CALIBER_DATABASE_URL")]:::store
    mdb[("MLflow backend store")]:::store
    cart[("CALIBER storage service<br/>local · S3<br/><i>MinIO via S3 compatibility</i>")]:::store
    mart[("MLflow artifact root<br/>MLflow-configured backends<br/><i>including S3 · MinIO · GCS · local</i>")]:::store
    llm["LLM providers<br/>OpenAI · Claude · MLflow AI Gateway"]:::ext

    spa -. "choose one topology" .-> eproc
    spa -. "choose one topology" .-> capi
    eproc --> cdb
    eproc --> mdb
    capi --> cdb
    mflow --> mdb
    eproc --> cart
    eproc --> mart
    capi --> cart
    mflow --> mart
    eproc --> llm
    capi --> llm

    classDef ui fill:#e0f2fe,stroke:#0284c7,color:#075985;
    classDef core fill:#ede9fe,stroke:#7c3aed,color:#4c1d95;
    classDef plug fill:#fce7f3,stroke:#db2777,color:#831843;
    classDef store fill:#eef2ff,stroke:#6366f1,color:#312e81;
    classDef ext fill:#f0fdf4,stroke:#16a34a,color:#14532d;
```

- **MLflow-integrated** — embedded mode registers CALIBER in the MLflow process; standalone mode exposes the same API/SPA and uses MLflow's HTTP APIs.
- **One control-plane codebase** — pollers, dispatchers, schedulers, calibration/refinement drains, and workflow/knowledge/assistant workers run with the CALIBER ASGI app; no separate worker tier is required.
- **Explicit state ownership** — CALIBER relational metadata and MLflow's backend store have separate configuration and migration owners; object storage holds file bytes.
- **Portable state** — PostgreSQL 17 (pgvector + Apache AGE) in production and SQLite for lightweight/dev. CALIBER storage supports local or S3 (including MinIO); MLflow artifact storage uses its independently configured backends, including GCS.

---

## 🚀 Quickstart

```bash
# ── Backend (from the repo root) ──
cd caliber
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,s3]"

# ── Frontend (in a second shell) ──
cd caliber/caliber-ui
npm install
npm run dev
```

Then start the backend from `caliber/`:

```bash
source .venv/bin/activate
make dev
```

`make dev` defaults MLflow artifacts to `s3://mlflow/mlruns` via the local MinIO endpoint
(`http://127.0.0.1:9000`) — start the suite's MinIO stack first, or override `MLFLOW_ARTIFACT_ROOT` /
`MLFLOW_S3_ENDPOINT_URL`.

🔗 Open the native-development UI at **`http://127.0.0.1:5000/caliber/login`**.

On a fresh local database, sign in with **username `admin` and password `admin`**. Change
the password immediately in **Administration**; the bootstrap runs only while the account
table is empty and never resets an existing password. This convenience bootstrap is enabled only by the
loopback launcher; any network-reachable deployment must leave
`CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=false` and supply a strong password source.

> 🐳 **Loopback-only container development stack** (Postgres 17, MinIO, MLflow, MLflow AI Gateway, and NATS): use [`deploy/`](./deploy/) — see [`deploy/README.md`](./deploy/README.md). It is not a production deployment template.

---

## 🗺️ Repository layout

| Path | What it is |
| --- | --- |
| 📦 [`caliber/`](./caliber/) | The Python package for both the in-process MLflow app and standalone CALIBER service — runtime code, Alembic migrations, the React SPA, tests, and packaging. See its [README](./caliber/README.md) for install + dev. |
| 🌐 [`docs-site/`](./docs-site/) | The published documentation site: the [landing page](./docs-site/index.html); 19 generated Markdown-backed module pages (built from [`docs/`](./docs/) by [`build-docs.mjs`](./docs-site/build-docs.mjs)); the **[Cookbooks](./docs-site/m-16-cookbooks.html)** — a card index plus 16 step-by-step, UI-only recipe pages sourced from [`docs-site/cookbooks/`](./docs-site/cookbooks/); the [walkthrough](./docs-site/walkthrough.html) runbook; and click-through slide decks. Also synced into the SPA and served in-app at `/caliber/docs/`. |
| 📐 [`docs/`](./docs/) | Source-of-truth design specs — one `architecture.md` per numbered area (platform, registries, workflows, data, observability, QA, evaluation, calibration, assistant) plus the [workflow components reference](./docs/06-workflows/components.md). Each opens with an **At a glance** summary + a typed-color diagram, then a banded **Reference** tier. Conventions in [`docs/STYLE.md`](./docs/STYLE.md); machine index for agents at `docs-site/llms.txt`. |
| 🐳 [`deploy/`](./deploy/) | A loopback-only Docker development stack (Postgres 17 + pgvector + Apache AGE, MinIO, MLflow, the MLflow AI Gateway, and NATS) plus `compose.yaml`. See [`deploy/README.md`](./deploy/README.md). |
| 🎬 [`overview-video/`](./overview-video/) | Narration script and screenshot/video-generation scripts that render the embedded overview video on the docs site. |

---

## 📚 Documentation

| Start here | For… |
| --- | --- |
| 🏠 **[Docs landing page](./docs-site/index.html)** | What CALIBER is, why it exists, and how it fits with MLflow |
| 🧭 **[Walkthrough runbook](./docs-site/walkthrough.html)** | A copy-pasteable bring-up that tours every SPA page and builds a governed artifact with Aria |
| 🧑‍🍳 **[Cookbooks](./docs-site/m-16-cookbooks.html)** | 16 step-by-step, UI-only recipes — prompt regression, precision skills, policy-safe tools, doc-to-JSON, governed MCP, grounded knowledge, support/incident copilots, self-healing workflows, observability & triage, trustworthy evaluation, release signoff, and four Aria goal-plan recipes |
| 🧱 **[Build the plugin](./caliber/README.md)** | `cd caliber && python -m pip install -e ".[dev,s3]"`, then `make dev` |
| 🤝 **[Contributing](./caliber/CONTRIBUTING.md)** | Local setup, the quality gate, extension seams, and PR conventions |

**Design context** — browse [`docs/`](./docs/) or the rendered [architecture pages](./docs-site/m-01-platform.html):
[platform architecture](./docs/01-caliber/) · [Aria copilot](./docs/12-assistant/) · [evaluation](./docs/14-evaluation/) + [calibration](./docs/15-calibration/).

---

## ⚡ MLflow 3.14 native

The suite tracks the current open-source **MLflow (`≥3.14`)** and **PostgreSQL 17** (pgvector + Apache AGE).
Five MLflow 3.14 GenAI capabilities are wired in with genuinely-native OSS APIs (native Review Queues are
Databricks-only, so that surface is built CALIBER-native on the OSS assessment primitives):

| Capability | How CALIBER uses it |
| --- | --- |
| **Test Set → dataset sync** | Push a test set to MLflow's native `mlflow.genai.datasets` registry for the dataset UI + source-trace lineage, while Postgres stays the source of truth (with a synced/stale badge). |
| **Custom LLM judges** | Author reusable judges from natural-language instructions (`mlflow.genai.make_judge`) on the **Judges** page; usable in the refinement gate, human review, and the Evaluations scorecard as `Judge.<id>` tokens alongside deterministic heuristics. |
| **Multimodal tracing** | Extraction spans carry the source document as a trace attachment so the KG/OCR pipeline's inputs render in the trace viewer. |
| **Trace retention** | Opt-in, server-owned archival of aged traces to object storage. |
| **Review Queues** | Define a label schema (pass/fail, categorical, numeric, free-text), enqueue traces, and review them; answers write back onto each trace as MLflow assessments/expectations. |

---

## ✅ Project status

The platform spans prompts, tools, skills, MCP servers, workflows, knowledge bases, object storage, test
sets, observability, evaluation, calibration, the LLM Gateway, RBAC, and Aria. For current evidence,
remaining defects, production-boundary limits, and the distinction between local verification and remote
release proof, read [`product-complete-report.md`](./product-complete-report.md). Historical test totals are
retained there with their dates and must not be read as a current full-suite result.

---

## 🧪 Testing & quality

<details>
<summary><b>Run the test suites</b></summary>

```bash
# Backend
cd caliber
ruff check src tests && mypy src
pytest -n auto -m "not integration"   # xdist parallel; integration tests need external servers

# Frontend
cd caliber/caliber-ui
npm run lint
npm run typecheck
npm run test:coverage
npm run test:e2e
```

</details>

<details>
<summary><b>Allure reports</b></summary>

All three test layers can emit [Allure](https://allurereport.org/) results:

| Layer | Emit results |
| --- | --- |
| Backend (pytest) | `cd caliber && make test-allure` |
| Frontend unit (vitest) | `cd caliber/caliber-ui && npm test` (emits automatically) |
| E2E (playwright) | `cd caliber/caliber-ui && npm run test:e2e` (emits + attaches screenshots/traces) |

**Emitting `allure-results/` needs no Java** — only *rendering* the HTML report does. Render from the suite root:

```bash
make allure-report   # run backend + UI unit + Playwright, then render one report
make allure          # render/open results that already exist
```

Rendering uses a local Java runtime when present and **falls back to a Dockerized JRE** otherwise, so it
works on any host with *either* Java or Docker (`make check` reports whether Java is available — it's
optional). Install Java directly with `brew install --cask temurin` (macOS),
`sudo apt-get install -y default-jre` (Debian/Ubuntu), or `choco install temurin` (Windows).

**CI:** results are produced without Java — upload `allure-results/` as an artifact, or render in-job by
adding Java first:

```yaml
- uses: actions/setup-java@v4
  with: { distribution: temurin, java-version: "21" }
- run: cd caliber/caliber-ui && npm run allure:generate:all
```

</details>

---

## ⚙️ Configuration

CALIBER is environment-configured through `CaliberConfig`. The **Settings** page shows a safe, grouped
inventory of assistant, LLM, storage, security, worker, webhook, and sandbox configuration. Common production
variables include `CALIBER_DATABASE_URL`, `CALIBER_ADMIN_USERS`, `CALIBER_WORKFLOW_STORAGE_*`,
`CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES`, and provider selections such as `CALIBER_LLM_PROVIDER`,
`CALIBER_EVAL_PROVIDER`, and `CALIBER_PROMOTER_PROVIDER`.

<details>
<summary><b>Single development environment (v1)</b></summary>

The initial product ships a **single environment**. A build deploys straight to one live alias — there is no
`dev → staging → prod` promotion ladder or stage selector. Environment classification still fails closed:
production-class aliases require an attached passing deploy gate graded by a real configured executor by
default. Human approval after a passing gate is separately off by default; enabling it creates a pending
promotion rather than rotating the alias immediately. The infrastructure is likewise single-stack (one
`deploy/compose.yaml`, one `.env`, one Postgres/MinIO/MLflow).

The workflow promotion state machine remains active for quality and graded-executor policy; only its
human-approval policy ships disabled. Prompt
approval governance is not merely disabled: the current prompt record is a born-approved provenance anchor,
so a future multi-environment mode must rebuild a pending/approve/reject path and requester/approver
distinction. The current constants show the relevant seams; changing them alone is not a supported activation:

- **Frontend:** flip `SINGLE_ENVIRONMENT` in [`caliber/caliber-ui/src/lib/environment.ts`](caliber/caliber-ui/src/lib/environment.ts).
- **Backend:** workflow aliases are defined around `GATED_ALIASES` in [`caliber/src/caliber/workflows/promoter.py`](caliber/src/caliber/workflows/promoter.py); prompt discovery aliases live in [`caliber/src/caliber/routes/prompts.py`](caliber/src/caliber/routes/prompts.py). A real activation also needs configuration, migrations, authorization, and compatibility tests.

</details>

---

## 🛠️ Troubleshooting

<details>
<summary><b>Common symptoms &amp; checks</b></summary>

| Symptom | Check |
| --- | --- |
| MinIO/S3 shows not configured | Set `CALIBER_WORKFLOW_STORAGE_BUCKET` plus endpoint, region, path-style, and credential-source variables; install the `s3` extra. |
| UI deep link returns 404 | Use the CALIBER backend `/caliber/` route or the Vite dev server with the correct `CALIBER_UI_BASE`. |
| Playwright cannot start | Run `npm run playwright:install` in `caliber/caliber-ui`. |
| Backend tests fail on Starlette TestClient | Ensure dev dependencies include `httpx2>=2.3` and reinstall with `pip install -e ".[dev]"`. |
| Published workflow service returns 413 | The raw JSON envelope exceeded `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` (1 MiB by default). Put large content in managed storage and pass a reference, or deliberately raise the deployment limit. |

</details>

---

## 🤝 Contributing

Contributions are welcome — start with **[CONTRIBUTING.md](./caliber/CONTRIBUTING.md)** for local setup, the quality
gate, the extension seams, and PR conventions.

## 📄 License

**Apache 2.0** — see [`caliber/LICENSE`](./caliber/LICENSE).

<div align="center"><sub>Built on MLflow · PostgreSQL · React — <a href="./docs-site/index.html">read the docs</a></sub></div>
