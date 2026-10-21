---
audience:
  - developer
  - operator
doc_type: tutorial
product_area: docs
stability: beta
prerequisites:
  - Python 3.10, 3.11 or 3.12 and the three CALIBER wheels (release assets or a source checkout)
reviewed_on: 2026-10-15
version_applicability: 0.1.0a1 alpha
tags:
  - install
  - alpha
  - onboarding
  - setup
---

# Alpha install

This page takes you from wheels to a logged-in administrator who can create a project and a
prompt. It needs no Docker. The install is checked by `scripts/verify-alpha-install.sh`.
Until the `caliber-v0.1.0a1` release assets are published, you need a source checkout to
build the wheels and to run that verification script.

CALIBER is alpha software. The packages are not on PyPI yet, so `pip install caliber-sdk`
returns "no matching distribution". Install from the wheels instead.

## What you get

| Piece | Where it runs |
| --- | --- |
| CALIBER (the control plane and API) | `uvicorn` on `127.0.0.1:5001` |
| MLflow (the prompt registry) | `mlflow server` on `127.0.0.1:5000`, its own SQLite file |
| CALIBER's database | one SQLite file, separate from MLflow's |

The LLM and the evaluator are simulated in this setup. That is the honest offline default:
calibration runs and returns candidates, but the evaluation scores are canned and every screen
that shows them says so. See [Real LLM and evaluation](#real-llm-and-evaluation) to change that.

## 1. Get the wheels

The release assets are not published yet. From a source checkout, build
`caliber_sdk`, `caliber_cli` and `caliber_suite` (all `0.1.0a1`) into `wheels/`:

```bash
python3.12 -m venv build-venv && build-venv/bin/pip install build
for pkg in sdk/caliber-sdk sdk/caliber-cli caliber; do
  build-venv/bin/python -m build --wheel --outdir wheels "$pkg"
done
```

A wheel built this way has no bundled web UI. To include the UI, use the build procedure in
[Releasing](../operate/releasing.md#cut-it). After the prerelease is published, you can
download its wheels instead of building them.

## 2. Create a virtual environment and install

Python 3.13 is not supported yet. Use 3.10, 3.11 or 3.12.

```bash
python3.12 -m venv alpha
source alpha/bin/activate
pip install --find-links ./wheels caliber-sdk caliber-cli caliber-suite
```

## 3. Migrate the database

The server never creates its own tables. Run the migration before the first start, and after
every upgrade:

```bash
export CALIBER_DATABASE_URL=sqlite:///$PWD/caliber.db
caliber-migrate
```

Without this step the server starts and `/health` looks fine, but every login fails with
`no such table: caliber_user_accounts`. `caliber-migrate` works from any directory and never
prints the database URL.

## 4. Start MLflow

In a second terminal, with the same virtual environment active:

```bash
mlflow server \
  --backend-store-uri sqlite:///$PWD/mlflow.db \
  --default-artifact-root $PWD/mlflow-artifacts \
  --host 127.0.0.1 --port 5000
```

Keep MLflow on its own SQLite file. Sharing one file with CALIBER works, but SQLite allows one
writer at a time, and the server warns at startup if you do.

## 5. Configure and start CALIBER

The server reads its configuration from the process environment. It does not read a `.env` file.

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export CALIBER_ADMIN_USERS=admin
export CALIBER_OPERATOR_USERS=admin
export CALIBER_APPROVER_USERS=admin
export CALIBER_PROMOTER_PROVIDER=mlflow
export CALIBER_ARTIFACT_STORE_PROVIDER=mlflow
export CALIBER_EVAL_PROVIDER=fake
export CALIBER_LOG_SINK=stderr
export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
uvicorn caliber.server:create_app --factory --host 127.0.0.1 --port 5001
```

What each group does:

- **Roles.** An account holds a password only. `CALIBER_ADMIN_USERS`, `CALIBER_OPERATOR_USERS` and
  `CALIBER_APPROVER_USERS` decide what that user may do. Leave all three empty and the first
  login is a viewer who cannot create a project. The server logs an error when that happens.
- **Providers.** The promoter and artifact store are the real MLflow ones, so Apply changes the
  registry. `CALIBER_EVAL_PROVIDER=fake` is written out so the simulated evaluator is a choice you
  can see.
- **Bootstrap login.** With the last two lines, the first start creates one account, `admin` with
  password `admin`. They are for a machine only you can reach. Change the password under
  Administration straight away, and never carry these lines into a network-reachable deployment.
  With neither a password source nor these lines, no account is created and the server logs a
  warning saying nobody can log in.

## 6. Verify

```bash
curl -s http://127.0.0.1:5001/ajax-api/2.0/mlflow/caliber/readiness
```

Expect `"ready":true`. The `simulated` list should contain at most `llm` and `eval`. If it lists
`promoter` or `artifact_store`, the release path is simulated and Apply will not change MLflow.

Then sign in and create a project:

```bash
B=http://127.0.0.1:5001/ajax-api/2.0/mlflow/caliber
curl -s -c jar -X POST $B/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}'
curl -s -b jar $B/auth/session                  # "is_admin": true
curl -s -b jar -X POST $B/projects -H 'content-type: application/json' \
  -d '{"name":"my-first-project"}'
curl -s -b jar -X POST $B/prompts -H 'content-type: application/json' \
  -d '{"name":"my-first-prompt","template":"Answer briefly: {{question}}"}'
```

With a wheel built using the bundled UI procedure, open `http://127.0.0.1:5001/caliber/`
and sign in as `admin` / `admin`. On an empty install the Dashboard shows a **New here? Start with the core loop**
panel with four steps (**Write a prompt**, **Add a test set**, **Run an evaluation**, **Release it**), each a link to the
page that does it.

For the Python SDK or `caliberctl`, create a token under **Settings → API tokens** in the UI and use it
as `CALIBER_TOKEN`. The command line cannot issue the first one.

## Real LLM and evaluation

Evidence you can trust needs a real evaluator and a real model. Install the extra, set the key,
and switch both providers:

```bash
pip install --find-links ./wheels "caliber-suite[llm]"
export OPENAI_API_KEY=sk-...
export CALIBER_LLM_PROVIDER=openai
export CALIBER_LLM_API_KEY_ENV=OPENAI_API_KEY
export CALIBER_EVAL_PROVIDER=mlflow
```

Restart CALIBER. `/readiness` then lists nothing under `simulated`. A real model with the fake
evaluator is refused at startup unless you set `CALIBER_ALLOW_SIMULATED_PROVIDERS=true`.

## Waits, approvals and queued runs

Three settings are off in the steps above. They decide whether a workflow run can pause:

```bash
export CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true
export CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED=true
export CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED=true
```

With them off, runs execute synchronously and a **Human Approval**, **Wait Until** or **Wait For Event**
node cannot pause the run. `GET /capabilities` reports `workflow_runs.approval_readiness.status` as
`configuration_required`, naming each missing setting under `blockers`, and the workflow palette marks
those nodes "Needs the run queue". Turning them on lets a run stop at an approval, an event or a time and
resume later. Each Human Approval node's own `required_role` still decides who may approve; with
`CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` unset, the person who started a run cannot approve it. The docker
stack sets all three.

## Knowledge bases need more than this page sets up

A knowledge base reads its sources from an S3-compatible object store and embeds them with a local model,
and neither comes with the steps above:

- **An object store.** Run MinIO or point at S3 (`CALIBER_OBJECT_STORE_ENDPOINT_URL` and credentials). Without
  one, creating a knowledge base answers 503 and names the object store.
- **The embedding libraries.** `pip install --find-links ./wheels "caliber-suite[knowledge,knowledge-local]"`.
  Without `knowledge-local` every embedding model shows as unavailable, with this extra named. The default docker
  image installs `knowledge` only, so add `knowledge-local` to `CALIBER_INSTALL_EXTRAS` when you build it.
- **The model.** The first build downloads it from Hugging Face; pre-cache it into `HF_HOME` on an offline host.
  The docker stack already points `HF_HOME` at the persistent `/data/huggingface` volume
  (`deploy/caliber/compose.yaml`), so a native install should set `HF_HOME` to somewhere durable too, or the
  model re-downloads on every fresh venv.
- **PostgreSQL with pgvector** only if you turn on ANN search; **Apache AGE** only for graph retrieval.

The full list, and what is and is not verified, is on [Knowledge bases](../use/knowledge-bases.md#before-the-first-step-what-a-knowledge-base-needs).

## Enabling preview areas

The install above leaves the preview areas **off**: MCP servers, OpenAPI integrations, cookbooks, the Aria
assistant and plans, and release-candidate signoff with workspace releases. Their navigation entries are
hidden and their routes answer 404 with `reason_code: feature_disabled`. To try one, export its variable
next to the other settings in step 5 and restart CALIBER:

```bash
export CALIBER_FEATURE_ARIA=true                # Aria assistant and plans (preview)
export CALIBER_FEATURE_INTEGRATIONS=true        # MCP servers and OpenAPI integrations (preview)
export CALIBER_FEATURE_COOKBOOKS=true           # Cookbooks (preview)
export CALIBER_FEATURE_RELEASE_CANDIDATES=true  # release-candidate signoff and workspace releases
```

Turn on only what you want to try. Each variable defaults to false, is read once at startup, and accepts
`true`, `1`, `yes` or `on`. Enabling one means you accept that area's preview behavior (it may change, a
signoff records a decision and deploys nothing, a workspace release evaluation never runs); see
[Feature flags](alpha-scope.md#feature-flags). Some cookbooks need more than the cookbooks flag, for
example the Aria labs need `CALIBER_FEATURE_ARIA`; each lab's README names its flags.

To confirm what the server picked up, sign in as in step 6 and read `features` from the capabilities
response. Each of the four ids carries `enabled`, `env_var`, `label` and `stability`:

```bash
curl -s -b jar $B/capabilities
```

## Docker instead

If you would rather run the whole stack in containers, use `make start` from a source checkout.
The container entrypoint runs the migration and sets the roles and providers for you. See
[Local bring-up](../operate/local-bring-up.md).

The four `CALIBER_FEATURE_*` variables pass through from the environment to the container and default to
false there as well. Set them in the suite-root `.env` (`.env.example` documents them), then run
`make start` again; the container reads them at startup. The preview areas are off in the stack until you do.

## Check it yourself

From a source checkout, this script runs the steps above in a scratch directory and fails if any
of them stops working:

```bash
scripts/verify-alpha-install.sh --python python3.12
# or against downloaded release wheels:
scripts/verify-alpha-install.sh --wheel-dir ./wheels
```

## If something is wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/readiness` is not ready with a `schema` blocker, or login returns 500 and the log says `no such table` | the migration never ran, or the database is behind this release | run `caliber-migrate` with the same `CALIBER_DATABASE_URL` and restart |
| Login works but "create project" is refused with 403 | no role list names you | set `CALIBER_ADMIN_USERS` and restart |
| `admin`/`admin` is rejected | no account was created | set the two bootstrap lines in step 5, or a password source, on a fresh database |
| Creating a prompt or applying answers 503 "MLflow tracking server unreachable" | MLflow is not running, or `MLFLOW_TRACKING_URI` is wrong | start MLflow (step 4) or correct the URI, then retry; nothing was changed |
| `/readiness` lists `promoter` | a `fake` promoter was configured | set `CALIBER_PROMOTER_PROVIDER=mlflow` |
| `/readiness` says "registered tool modules are unrestricted" | `CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST` is empty, so any importable module can be registered as a tool | set it to the module prefixes you trust, for example `caliber.*,mycompany.tools.*`; see [Who is trusted to write tools](../use/tools.md#who-is-trusted-to-write-tools) |
| `pip install caliber-sdk` finds nothing | the packages are not on PyPI | install from the wheels as in step 2 |

## Next

- [5-minute quickstart](five-minute-quickstart.md)
- [Skills: the journey step by step](../use/skills.md#the-skill-journey-step-by-step)
- [Tools: the journey, and who is trusted to write them](../use/tools.md#the-tool-journey-step-by-step)
- [Local bring-up](../operate/local-bring-up.md)
- [Configuration and provider setup](../operate/configuration-and-provider-setup.md)
