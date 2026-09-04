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
[![MLflow](https://img.shields.io/badge/MLflow-≥3.15-0194E2?logo=mlflow&logoColor=white)](https://mlflow.org)
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
explicitly reviews before any alias update. Evaluation gates enforce whether refinement jobs advance to
`candidate_ready`; separately persisted per-version verdicts are advisory release evidence and never block
prompt alias rotation. Audited rollback exists only where an asset records live-target or snapshot history,
and its semantics remain asset-specific. The implementation reuses MLflow's
Experiments, Traces, Assessments, Prompt Registry, Artifact Store, and `genai.evaluate`. It runs as a standalone ASGI service that integrates with MLflow over HTTP via `MLFLOW_TRACKING_URI`. The embedded `mlflow.app` mode is unsupported as a product or developer path.

> 📖 **Live docs:** [`docs-site/index.html`](./docs-site/index.html) &nbsp;·&nbsp; 🏛️ **Layered architecture:** [HTML](./docs-site/m-00-layered-architecture.html) / [Markdown](./ARCHITECTURE.md) &nbsp;·&nbsp; 🧑‍🍳 **Learn by doing:** [16 Cookbooks](./docs-site/m-16-cookbooks.html)
>
> 🌍 **Published:** [rrahimi-uci.github.io/caliber-suite](https://rrahimi-uci.github.io/caliber-suite/) &nbsp;·&nbsp; 🧪 **Test report:** [`/tests/`](https://rrahimi-uci.github.io/caliber-suite/tests/) — the Allure report from the most recent successful `main` CI run
>
> 🎬 **[Play the narrated CALIBER overview](https://rrahimi-uci.github.io/caliber-suite/overview-video.html)** — watch and listen to a guided introduction to the platform in your browser. [Read the narration](./overview-video/narration_script.md) if you prefer the script.

---

## 🧭 Design principles

Ten principles govern what CALIBER builds and what it refuses to build, ordered
deliberately — purpose and trust first, then capability and architecture, then
operational quality, and finally technology choices and longevity. When two pull
against each other, the lower number wins.

| # | Principle | |
| --- | --- | --- |
| 1 | **Governed Agentic Workflows** | Governance, security, compliance, and policy enforcement at the core. |
| 2 | **Progressive Autonomy** | The path from human-led processes to fully autonomous agent-driven execution. |
| 3 | **Auditability & Observability** | Every workflow, decision, and agent action traceable, transparent, and explainable. |
| 4 | **Evaluation by Design** | Testing, verification, validation, benchmarking, calibration, and optimization from the start. |
| 5 | **Open & Extensible** | Enterprise systems, AI models, tools, and external agent platforms through open APIs and SDKs. |
| 6 | **Modular & Composable** | Loosely coupled components that are reusable, flexible, and easy to extend. |
| 7 | **Scalable & Reliable** | Enterprise-grade performance, resilience, and operational reliability. |
| 8 | **Open-Source First** | Proven open-source building blocks with strong community support. |
| 9 | **Developer & User Friendly** | A clear, productive experience for both developers and business users. |
| 10 | **Future Ready** | Evolving AI capabilities, orchestration patterns, and enterprise needs. |

[`ARCHITECTURE.md`](./ARCHITECTURE.md#design-principles) is the canonical
statement, and maps each principle to the mechanism that discharges it — including
where a mechanism is partial and says so.

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
    g["③ Optimize<br/>policy-selected optimizer"]:::auto
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

**Why it matters** — changes reuse MLflow evidence, must clear an enforced candidate-advancement gate,
and carry per-dimension evaluation plus any advisory per-version verdict into review. Promotion
authorization remains explicit; the persisted advisory verdict never blocks prompt alias rotation.
Automatic multi-agent bundle optimization remains roadmap work rather than a shipped optimizer.

---

## 🧩 What's inside

A single control plane spanning the whole agent stack. Implementation and verification status varies by
surface; the repository audit records the exact current boundary.

| Area | Highlights |
| --- | --- |
| ✍️ **Authoring** | **Prompts**, **Tools**, **Skills**, **MCP servers** — governed definitions with asset-specific testing and governance; versioning, gates, live aliases, and rollback apply only where each asset implements them |
| 🔀 **Workflows** | Visual **Studio**, queued runs, runtime approvals, checkpointing, and workflow-as-a-service |
| 📚 **Knowledge bases** | Versioned RAG corpora — chunking, embeddings, **Apache AGE** graph extraction, hybrid retrieval + cross-encoder rerank |
| 🧪 **Evaluation** | Scorecards, **custom LLM judges** (`make_judge`), structured human-review queues, enforced refinement-advancement gates, and advisory persisted version verdicts |
| 🎛️ **Calibration** | Five implemented provider paths: the prompt form exposes MetaPrompt and GEPA; policy can select SkillMetaPrompt and DSPy BootstrapFewShot; DSPy MIPRO requires explicit configuration |
| 🔭 **Observability** | MLflow tracing with multimodal attachments, SSE live events, service visibility, and trace retention |
| 🛡️ **Governance** | RBAC, path-specific audited promotion/rollback, and the **LLM Gateway** surface (endpoint discovery, guardrails, per-model pricing, usage) |
| 🤖 **Aria copilot** | An embedded, permissioned **agentic tool loop** (OpenAI + Claude) with durable goal-plans, async resume, and self-correction; plan gates support permission prompts and separation of duties where an operation is routed through a gated step |
| 🧰 **SDK & CLI** | A typed Python client (`caliber-sdk`) with 100% coverage of the addressable management API, gated in CI so a new server route cannot ship without SDK coverage; a non-interactive operator CLI (`caliberctl`) covering the release-governance verbs (promote, rollback, gate verdicts); and a versioned deprecation policy. See [`sdk/`](./sdk/) |

---

## 🏢 Architecture

> 🏛️ **For the layered, executive-and-architect view — the six-layer stack, the canonical chain, the
> anatomy of a governed asset, and the per-asset guarantees — read
> [`ARCHITECTURE.md`](./ARCHITECTURE.md) or its [generated HTML page](./docs-site/m-00-layered-architecture.html).**
> The section below is the deployment-topology summary.

CALIBER runs as a **standalone ASGI service** that integrates with a separate vanilla MLflow server over HTTP. The bundled loopback Compose stack runs CALIBER on `:5001` and MLflow on `:5000`; `CALIBER_DATABASE_URL` is independent of MLflow's backend store. Embedded `mlflow.app` mode is unsupported as a product or developer path — see [ARCHITECTURE.md](./ARCHITECTURE.md) for the support matrix.

```mermaid
flowchart TB
    spa["React SPA — /caliber/"]:::ui

    subgraph standalone["Topology B · bundled standalone service"]
        capi["CALIBER ASGI :5001<br/>API · SPA · in-process workers"]:::plug
        mflow["MLflow :5000<br/>traces · registry · evaluate"]:::core
        capi -->|"MLFLOW_TRACKING_URI / HTTP"| mflow
    end

    embedded["⚠️ Topology A · embedded mlflow.app (unsupported)<br/><i>internal compatibility only</i>"]:::plug
    cdb[("CALIBER metadata DB<br/>CALIBER_DATABASE_URL")]:::store
    mdb[("MLflow backend store")]:::store
    cart[("CALIBER storage service<br/>local · S3<br/><i>MinIO via S3 compatibility</i>")]:::store
    mart[("MLflow artifact root<br/>MLflow-configured backends<br/><i>including S3 · MinIO · GCS · local</i>")]:::store
    llm["LLM providers<br/>OpenAI · Claude · MLflow AI Gateway"]:::ext

    spa --> capi
    capi --> cdb
    mflow --> mdb
    capi --> cart
    mflow --> mart
    capi --> llm

    classDef ui fill:#e0f2fe,stroke:#0284c7,color:#075985;
    classDef core fill:#ede9fe,stroke:#7c3aed,color:#4c1d95;
    classDef plug fill:#fce7f3,stroke:#db2777,color:#831843;
    classDef store fill:#eef2ff,stroke:#6366f1,color:#312e81;
    classDef ext fill:#f0fdf4,stroke:#16a34a,color:#14532d;
```

- **MLflow-integrated** — CALIBER runs as an independent service and integrates with MLflow exclusively over HTTP via `MLFLOW_TRACKING_URI`. Embedded `mlflow.app` mode is not a supported path.
- **One control-plane codebase** — pollers, dispatchers, schedulers, calibration/refinement drains, and workflow/knowledge/assistant workers run with the CALIBER ASGI app; no separate worker tier is required.
- **Explicit state ownership** — CALIBER relational metadata and MLflow's backend store have separate configuration and migration owners; object storage holds file bytes.
- **Portable state** — PostgreSQL 17 (pgvector + Apache AGE) in production and SQLite for lightweight/dev. CALIBER storage supports local or S3 (including MinIO); MLflow artifact storage uses its independently configured backends, including GCS.

---

## 🚀 Quickstart

```bash
# ── Backing services (terminal 1, from the repo root) ──
# Starts vanilla MLflow and its Postgres/MinIO dependencies, but not the
# containerized CALIBER service that would occupy :5001.
docker compose -f deploy/compose.yaml --profile app up -d mlflow
```

Then start standalone CALIBER from `caliber/` in a second terminal:

```bash
cd caliber
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,s3,postgres]"
export CALIBER_DATABASE_URL=postgresql+psycopg://caliber:caliber@127.0.0.1:5432/caliber
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
uvicorn caliber.server:create_app --factory --reload --host 127.0.0.1 --port 5001
```

For frontend HMR, use a third shell:

```bash
cd caliber/caliber-ui
npm install
npm run dev
```

The supported local development path keeps CALIBER separate from its backing services. Compose runs
Postgres, MinIO, and vanilla MLflow; Uvicorn serves CALIBER on `:5001`; and Vite proxies `/ajax-api/*`
to that CALIBER process by default. The insecure bootstrap flags above are loopback-only convenience
settings: change the first-boot password immediately and never carry them into a network-reachable deployment.

🔗 Open the native-development UI at **`http://127.0.0.1:5001/caliber/`**.

## Supported v1 journey: governed prompt refinement

CALIBER's canonical v1 path is **governed prompt refinement**: production trace → flagged assessment →
verify → diagnose → optimize → evaluate → apply → promote. This is the end-to-end reference journey for
v1; other asset families implement subsets of that lifecycle. The supported local development path runs
standalone CALIBER (`uvicorn caliber.server:create_app --factory --reload`) separately from its backing
services, which can run via Compose.

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
| 📦 [`caliber/`](./caliber/) | The Python package for the standalone CALIBER service — runtime code, unsupported embedded-compatibility coverage, Alembic migrations, the React SPA, tests, and packaging. See its [README](./caliber/README.md) for install + dev. |
| 🧰 [`sdk/`](./sdk/) | Three independently-versioned, independently-installable packages: [`caliber-sdk`](./sdk/caliber-sdk/) (the typed Python client — zero server dependencies, gated at 100% addressable API coverage), [`caliber-cli`](./sdk/caliber-cli/) (`caliberctl`, a non-interactive operator CLI built on `caliber-sdk` alone), and [`caliber-plugin-sdk`](./sdk/caliber-plugin-sdk/) (experimental contracts for third-party optimizer extensions, with no dependency on the server or client SDK). |
| 🌐 [`docs-site/`](./docs-site/) | The published documentation site: the [landing page](./docs-site/index.html); the generated HTML/Markdown documentation corpus built from root [`ARCHITECTURE.md`](./ARCHITECTURE.md) plus [`docs/`](./docs/) by [`build-docs.mjs`](./docs-site/build-docs.mjs); the **[Cookbooks](./docs-site/m-16-cookbooks.html)** — a card index plus 16 step-by-step recipe pages sourced from [`docs-site/cookbooks/`](./docs-site/cookbooks/); the [walkthrough](./docs-site/walkthrough.html) runbook; and click-through slide decks. Also synced into the SPA and served in-app at `/caliber/docs/`. |
| 📐 [`docs/`](./docs/) | Source-of-truth documentation for architecture, build/integration, operations, use-case guides, SDK/API reference, strategy, and dated audit reports. The architecture corpus still centers on per-area `architecture.md` files plus the [workflow components reference](./docs/06-workflows/components.md); the broader docs tree also includes start/build/use/operate/sdk/api guides, roadmap material, and repository reports. Conventions live in [`docs/STYLE.md`](./docs/STYLE.md); the machine index is `docs-site/llms.txt`. |
| 📄 [`paper/`](./paper/) | LaTeX source for the technical report *CALIBER: A Layered Control Plane for Governed Releases of AI-Agent Resources* — the architectural argument behind this repository, its per-family guarantee surface, and an explicit account of what it does **not** establish. Also holds the generated [seminar deck](./paper/slides/). See [`paper/README.md`](./paper/README.md). |
| 🐳 [`deploy/`](./deploy/) | A loopback-only Docker development stack (Postgres 17 + pgvector + Apache AGE, MinIO, MLflow, the MLflow AI Gateway, and NATS) plus `compose.yaml`. See [`deploy/README.md`](./deploy/README.md). |
| 🎬 [`overview-video/`](./overview-video/) | Narration script and screenshot/video-generation scripts that render the embedded overview video on the docs site. |

---

## 📚 Documentation

| Start here | For… |
| --- | --- |
| 🏠 **[Docs landing page](./docs-site/index.html)** | What CALIBER is, why it exists, and how it fits with MLflow |
| 🏛️ **[Layered architecture](./docs-site/m-00-layered-architecture.html)** | Executive and architect map of the stack, governance chain, asset-family guarantees, topologies, execution, state, and trust boundaries |
| 🚨 **[Operations runbook](./docs/runbook.md)** | On-call procedure for the recoveries CALIBER cannot complete alone — unsettled release intents, indeterminate external effects, a queue that stopped draining, per-family rollback — and what each triage surface does *not* prove |
| 🧭 **[Walkthrough runbook](./docs-site/walkthrough.html)** | A copy-pasteable bring-up that tours every SPA page and builds a governed artifact with Aria |
| 🧑‍🍳 **[Cookbooks](./docs-site/m-16-cookbooks.html)** | 16 step-by-step, UI-only recipes — prompt regression, precision skills, policy-safe tools, doc-to-JSON, governed MCP, grounded knowledge, support/incident copilots, self-healing workflows, observability & triage, trustworthy evaluation, release signoff, and four Aria goal-plan recipes |
| 🎯 **[v1 product requirements](./docs/prd.md)** | The bounded v1.0.0 product contract: target users, supported prompt-refinement journey, non-goals, release requirements, and traceability to the GitHub delivery plan |
| 🧰 **[SDK guide](./docs/sdk/guide.md)** | Install, authenticate, and drive `caliber-sdk` end to end — authoring vs. deploying, evidence and scoring, running workflows, deployment promote/rollback and gate verdicts, and error handling. Every code block is extracted from a tested example in [`sdk/caliber-sdk/examples/`](./sdk/caliber-sdk/examples/), not hand-written prose. See also [`caliberctl` and the async client](./docs/sdk/cli.md) and [`VERSIONING.md`](./sdk/caliber-sdk/VERSIONING.md) for the stability/deprecation policy. |
| 📄 **[Technical paper](./paper/README.md)** | The architectural argument: the governed asset, the governance chain, why guarantees are per-family rather than uniform, and an explicit register of what the work does not establish |
| 🎤 **[Seminar deck](./paper/slides/)** | A generated 25-slide walkthrough of the paper, with speaker notes and PNG proof sheets |
| 🧱 **[Run CALIBER locally](./caliber/README.md)** | `cd caliber && python -m pip install -e ".[dev,s3]"`, then `uvicorn caliber.server:create_app --factory --reload --host 127.0.0.1 --port 5001` |
| 🤝 **[Contributing](./caliber/CONTRIBUTING.md)** | Local setup, the quality gate, extension seams, and PR conventions |

**Design context** — start with the rendered [layered architecture](./docs-site/m-00-layered-architecture.html), then browse [`docs/`](./docs/) or the deeper [architecture pages](./docs-site/m-01-platform.html):
[platform architecture](./docs/01-caliber/) · [Aria copilot](./docs/12-assistant/) · [evaluation](./docs/14-evaluation/) + [calibration](./docs/15-calibration/).

---

## ⚡ MLflow-native

The suite tracks the current open-source **MLflow (`≥3.15`)** and **PostgreSQL 17** (pgvector + Apache AGE).
Five GenAI capabilities from the MLflow 3.14 line are wired in with genuinely-native OSS APIs (native
Review Queues are Databricks-only, so that surface is built CALIBER-native on the OSS assessment
primitives):

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
sets, observability, evaluation, calibration, the LLM Gateway, RBAC, and Aria. The dated
[`docs/reports/product-completeness-report.md`](./docs/reports/product-completeness-report.md) preserves the
historical implementation and validation record. Its test totals are not current release proof; use the
latest `main` CI run for the current checkout.

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

OpenAI-backed model calls default consistently to `gpt-5.6-luna` with `high`
reasoning across agents, workflows, evaluation, calibration, memory, knowledge,
and the assistant. Operators can still override the model or reasoning budget
through the documented environment, asset, and call-level controls.

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
