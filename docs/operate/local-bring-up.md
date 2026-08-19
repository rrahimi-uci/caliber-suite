---
audience:
  - operator
  - developer
doc_type: tutorial
product_area: operations
stability: ga
prerequisites:
  - Repository checkout
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - setup
  - local
  - readiness
  - operations
---

# Local bring-up

This page is the operator-first path for starting CALIBER locally and verifying
that the runtime is healthy before you use deeper feature or architecture docs.

## At a glance

| Path | Use when | Entry point |
| --- | --- | --- |
| Full suite | You want the complete local stack around CALIBER | `make setup`, `make start` |
| Standalone dev | You want the shortest supported CALIBER edit/test loop inside `caliber/` | `docker compose ... --profile app up -d mlflow`, then `uvicorn caliber.server:create_app --factory --reload --port 5001` |
| Readiness check | You want to confirm dependency posture after boot | `GET /ajax-api/2.0/mlflow/caliber/readiness` |
| Recovery path | You need failure handling, not first boot | [Operations runbook](../runbook.md) |

## 1. Full suite path

Use the full suite when you want the local product as it is usually demonstrated:
CALIBER, MLflow, storage, and the surrounding services together.

```bash
make setup
make start
```

Then verify:

- CALIBER UI loads
- login succeeds
- readiness reports a usable local posture

## 2. Standalone development path

Use the standalone path when you are developing CALIBER itself and want the
shorter supported inner loop. Start vanilla MLflow and its dependencies from the
repository root without starting the containerized CALIBER service:

```bash
docker compose -f deploy/compose.yaml --profile app up -d mlflow

cd caliber
make install
export CALIBER_DATABASE_URL=postgresql+psycopg://caliber:caliber@127.0.0.1:5432/caliber
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
uvicorn caliber.server:create_app --factory --reload --host 127.0.0.1 --port 5001
```

Optional frontend dev loop:

```bash
cd caliber/caliber-ui
npm install
npm run dev
```

Vite proxies API calls to the standalone CALIBER process on `:5001` by default.
The insecure bootstrap flags are for trusted loopback use only; change the
first-boot password immediately and never use them for a network-reachable deployment.

## 3. Health and readiness

After either path, do not assume the runtime is usable just because the process
started.

Verify both:

- liveness: `/health`
- readiness: `/ajax-api/2.0/mlflow/caliber/readiness`

The readiness endpoint is the meaningful one for operator decisions.

Then move to:

- [Health and readiness](../operate/health-and-readiness.md)
- [Configuration and provider setup](../operate/configuration-and-provider-setup.md)

## 4. First local login

The local bootstrap path is only for loopback bring-up. Rotate any insecure
bootstrap defaults before exposing the deployment beyond the local machine.

For the deeper trust boundary and scope model, use:

- [Platform](../01-caliber/architecture.md)
- [Operations runbook](../runbook.md)

## 5. After boot

Once the system is up, the next doc depends on your job:

- using the product: [Choose your CALIBER path](../start/choose-your-path.md)
- integrating programmatically: [Auth and project scoping](../build/auth-and-project-scoping.md)
- operating and recovering: [Operator troubleshooting](../operate/troubleshooting.md), [Operations runbook](../runbook.md)
- validating trust and governance: [Trust and governance](../use/trust-and-governance.md)
