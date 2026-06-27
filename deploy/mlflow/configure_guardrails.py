#!/usr/bin/env python3
"""Configure MLflow **gateway guardrails** on a running MLflow 3.13 server.

What this does
--------------
MLflow's gateway enforces guardrails inline on every LLM call that flows through
a gateway endpoint (``mlflow/server/gateway_api.py`` runs pre-LLM and post-LLM
guardrails). Each guardrail is backed by an MLflow **scorer**. This script:

1. registers the guardrail scorers (``ToxicLanguage`` / ``DetectPII`` /
   ``DetectJailbreak`` — from ``mlflow.genai.scorers.guardrails``, which require
   the ``guardrails-ai`` package + Hub validators baked into the MLflow image),
2. creates gateway guardrails over them — jailbreak/toxicity/PII as
   ``BEFORE``/``VALIDATION`` (block bad input), PII as ``AFTER``/``SANITIZATION``
   (redact the output),
3. attaches each guardrail to every gateway endpoint.

Since CALIBER routes all LLM traffic through this gateway (``CALIBER_LLM_BASE_URL``),
the guardrails then apply to CALIBER agents + refinement stages with no change to
CALIBER itself.

⚠️  STATUS — UNVERIFIED SCAFFOLDING.
MLflow's gateway-guardrail API is new in 3.13 and exposed only at the
tracking-store level (no public ``MlflowClient`` method; entities are
``workspace``-scoped). The call sequence below is written against the confirmed
store signatures, but it has NOT been run end-to-end here — validate it against
your live MLflow 3.13 build and adjust method names/args as needed. Run it
*inside the MLflow container* (it needs ``guardrails-ai`` to instantiate the
scorers) or any env with ``guardrails-ai`` installed, pointing at the server:

    MLFLOW_TRACKING_URI=http://localhost:5000 python configure_guardrails.py

Requires the Hub validators to be installed in the image (see Dockerfile
``GUARDRAILS_HUB_TOKEN``).
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("configure_guardrails")


def _store(tracking_uri: str):
    """Return the RestStore (it carries the gateway-guardrail mixin methods)."""
    import mlflow

    client = mlflow.MlflowClient(tracking_uri=tracking_uri)
    # The gateway methods live on the tracking store (RestStore for an http URI),
    # not on the public client — this reach-through is deliberate.
    return client._tracking_client.store  # noqa: SLF001 - internal API by necessity


def _build_scorers():
    """Instantiate the guardrail scorers (needs guardrails-ai + Hub validators)."""
    from mlflow.genai.scorers.guardrails import (  # noqa: PLC0415
        DetectJailbreak,
        DetectPII,
        ToxicLanguage,
    )

    # name -> (scorer, [(stage, action), ...])
    return {
        "jailbreak": (DetectJailbreak(), [("BEFORE", "VALIDATION")]),
        "toxicity": (
            ToxicLanguage(),
            [("BEFORE", "VALIDATION"), ("AFTER", "VALIDATION")],
        ),
        "pii": (DetectPII(), [("BEFORE", "VALIDATION"), ("AFTER", "SANITIZATION")]),
    }


def main() -> int:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    log.info("configuring gateway guardrails on %s", tracking_uri)

    from mlflow.entities.gateway_guardrail import GuardrailAction, GuardrailStage

    store = _store(tracking_uri)

    # Existing guardrails (idempotency) + endpoints to protect.
    try:
        existing = {g.name: g for g in store.list_gateway_guardrails()}
        endpoints = list(store.list_gateway_endpoints())
    except Exception as exc:  # API shape differs / server too old
        log.error("could not reach the gateway-guardrail API: %s", exc)
        log.error("Confirm the server is MLflow >=3.13 and the gateway is enabled.")
        return 1

    if not endpoints:
        log.warning(
            "no gateway endpoints registered — nothing to attach guardrails to."
        )

    scorers = _build_scorers()
    created: list = []
    for name, (scorer, stage_actions) in scorers.items():
        # Register the scorer (returns its version); resolve its id via list_scorers.
        version = store.register_scorer(experiment_id=None, scorer=scorer)
        scorer_id = next(
            (
                s.scorer_id
                for s in store.list_scorers(experiment_id=None)
                if s.name == scorer.name
            ),
            None,
        )
        if scorer_id is None:
            log.warning("could not resolve scorer_id for %s — skipping", name)
            continue
        for stage, action in stage_actions:
            gname = f"caliber-{name}-{stage.lower()}"
            if gname in existing:
                created.append(existing[gname])
                continue
            guardrail = store.create_gateway_guardrail(
                name=gname,
                scorer_id=scorer_id,
                scorer_version=version,
                stage=GuardrailStage[stage],
                action=GuardrailAction[action],
            )
            created.append(guardrail)
            log.info("created guardrail %s (%s/%s)", gname, stage, action)

    # Attach every guardrail to every endpoint.
    for endpoint in endpoints:
        endpoint_id = getattr(endpoint, "endpoint_id", None) or getattr(
            endpoint, "id", None
        )
        for order, guardrail in enumerate(created):
            try:
                store.add_guardrail_to_endpoint(
                    endpoint_id=endpoint_id,
                    guardrail_id=guardrail.guardrail_id,
                    execution_order=order,
                )
                log.info("attached %s -> endpoint %s", guardrail.name, endpoint_id)
            except Exception as exc:  # already attached / API mismatch
                log.warning(
                    "attach %s -> %s failed: %s", guardrail.name, endpoint_id, exc
                )

    log.info("done: %d guardrail(s) configured.", len(created))
    return 0


if __name__ == "__main__":
    sys.exit(main())
