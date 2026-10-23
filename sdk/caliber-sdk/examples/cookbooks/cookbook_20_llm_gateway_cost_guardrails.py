"""Cookbook 20: meter every agent LLM call through one gateway, and put a price on it.

Runs the whole lab against a real CALIBER server and a real stand-in gateway (the lab's own
``assets/server/llm_gateway.py``, an OpenAI-compatible gateway CALIBER calls over real loopback
HTTP) and exits 0 only when every documented outcome was observed:

* the **LLM Gateway** page's view of the world: configured, reachable, two discovered endpoints,
  and "routing through gateway" true because CALIBER's own LLM base URL points at it -- then the
  gateway pauses and the same surface degrades to "unreachable" with the error, never a failure;
* the platform boots with a deliberately wrong provider key: the first workflow run fails at the
  gateway (401), and the gateway's own ledger records the refused call -- then an admin fixes the
  key at runtime through ``PATCH /settings/llm`` (admin-only, audited, fingerprint-masked, never
  readable back) and the same run succeeds;
* **pricing**: an operator prices the gateway's model per 1K tokens; a duplicate row is refused
  (409), a viewer cannot create one (403), and only an admin can update one;
* **exact cost**: the stand-in bills a flat 1000 prompt + 1000 completion tokens per call, so at
  $1/$2 per 1K every call costs exactly $3.00 -- and ``/gateway/usage`` says so, with the
  gateway's own ledger agreeing on the number of calls;
* **the meter is written at call time**: raising the price afterwards does not change what past
  traces cost; only the next call is billed at the new rate;
* the **secrets** lifecycle: admin-only put/rotate/revoke/delete, version bumps on rotation, the
  value never readable back -- the governed home for a gateway key (the README shows how to boot
  the platform with a ``secret://`` key source);
* **guardrail definitions**: list the catalog, create a deterministic (regex) guardrail, see it
  listed with its coverage, delete it -- with the honest caveat that enforcement happens in
  MLflow's own AI Gateway, not in the standalone endpoint listing on this page;
* the **audit trail** for every governed write above.

Environment: ``CALIBER_BASE_URL`` and ``CALIBER_TOKEN`` (an admin token). ``LAB20_GATEWAY_PORT``
(default 8791) says where the stand-in gateway listens; ``LAB20_GATEWAY_KEY`` (default
"lab20-gateway-key") is the bearer key its chat route demands; ``LLM_GATEWAY_LEDGER`` (the same
path the stand-in was started with, or ``CALIBER_STATE_DIR``/``lab20-gateway-ledger.jsonl``) is
where the script reads the gateway's own call log. Optional ``LAB20_SUFFIX`` makes names unique
for a re-run. ``--cleanup`` removes what a run created.

The CALIBER server must be started as the lab README describes: ``--real-llm`` with
``OPENAI_API_KEY=lab20-boot-key-wrong`` (the deliberately wrong boot key), ``CALIBER_LLM_BASE_URL``
pointing at the stand-in's ``/v1``, ``CALIBER_GATEWAY_URI`` at its root, and
``CALIBER_SECRET_ENCRYPTION_KEY_SOURCE`` naming an env var that holds a 32-byte key.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberError

LAB_DIR = Path(__file__).resolve().parents[3].parent / "docs-site/cookbooks"
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"

OPERATOR = ("lab20-operator", "lab20-operator-password")
VIEWER = ("lab20-viewer", "lab20-viewer-password")

PROMPT_PRICE = 1.0  # USD per 1K prompt tokens
COMPLETION_PRICE = 2.0  # USD per 1K completion tokens
TOKENS_PER_CALL = 2000  # the stand-in bills a flat 1000 + 1000 per call
COST_PER_CALL = 3.0  # (1000/1000)*1 + (1000/1000)*2


def say(message: str) -> None:
    print(message, flush=True)


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    say(f"  ok  {message}")


def expect_status(status: int, action: Any, message: str) -> CaliberAPIError:
    try:
        action()
    except CaliberAPIError as exc:
        check(exc.status_code == status, f"{message} -> HTTP {exc.status_code}")
        return exc
    raise AssertionError(f"{message}: expected HTTP {status}, the call succeeded")


def gateway_base() -> str:
    port = os.environ.get("LAB20_GATEWAY_PORT", "8791")
    return f"http://127.0.0.1:{port}"


def gateway_key() -> str:
    return os.environ.get("LAB20_GATEWAY_KEY", "lab20-gateway-key")


def ledger_path() -> Path:
    explicit = os.environ.get("LLM_GATEWAY_LEDGER")
    if explicit:
        return Path(explicit)
    state = os.environ.get("CALIBER_STATE_DIR")
    if not state:
        raise SystemExit("set CALIBER_STATE_DIR (source the env.sh) or LLM_GATEWAY_LEDGER")
    return Path(state) / "lab20-gateway-ledger.jsonl"


def ledger_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def gateway_calls(path: Path, *, ok: bool) -> int:
    """How many chat calls the gateway itself recorded (read from its own ledger)."""
    return sum(
        1 for e in ledger_entries(path) if e["path"] == "/v1/chat/completions" and e["ok"] is ok
    )


def pause_gateway() -> None:
    httpx.post(f"{gateway_base()}/admin/pause").raise_for_status()


def resume_gateway() -> None:
    httpx.post(f"{gateway_base()}/admin/resume").raise_for_status()


def reset_gateway() -> None:
    httpx.post(f"{gateway_base()}/admin/reset").raise_for_status()


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 120.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": f"cookbook-20-{user}"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_accounts(admin: CaliberClient) -> None:
    for user, password in (OPERATOR, VIEWER):
        try:
            admin.auth.accounts.create(user, password)
            say(f"  created account {user}")
        except CaliberError:
            admin.auth.accounts.update(user, password=password)
            say(f"  account {user} already existed; password reset to the lab value")


def workflow_manifest() -> str:
    return (
        LAB_DIR / "20-llm-gateway-cost-guardrails-lab/assets/workflow/metered-reply.workflow.yaml"
    ).read_text()


def by_model_row(usage: dict[str, Any], model: str) -> dict[str, Any] | None:
    return next((row for row in usage.get("by_model", []) if row["model"] == model), None)


def settled_usage(
    admin: CaliberClient, model: str, *, min_calls: int, timeout: float = 45.0
) -> dict[str, Any]:
    """Poll /gateway/usage until the model's call count reaches ``min_calls``.

    Spans land in MLflow asynchronously, so the first read after a run can be a
    beat early. Polling for the expected count (not sleeping blindly) keeps the
    script deterministic without hiding how usage is actually produced.
    """
    deadline = time.monotonic() + timeout
    row: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        row = by_model_row(admin.gateway.usage(since_ms=0), model)
        if row is not None and row["calls"] >= min_calls:
            return row
        time.sleep(1.0)
    raise AssertionError(
        f"usage for {model!r} did not reach {min_calls} call(s) within {timeout:.0f}s "
        f"(last seen: {row})"
    )


def run(
    admin: CaliberClient,
    *,
    operator: CaliberClient,
    viewer: CaliberClient,
    ledger: Path,
    suffix: str = "",
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("")
    reset_gateway()
    model_id = f"lab-chat-mini{suffix}" if suffix else "lab-chat-mini"
    # The pricing table matches on model_id (longest prefix); a suffixed re-run still uses the
    # manifest's "lab-chat-mini" model, so pricing rows are keyed per run via tags instead.
    model_id = "lab-chat-mini"

    say("== 1. workspace and people")
    project = admin.projects.create(
        f"gateway-ops{suffix}", description="Cookbook 20 metered gateway"
    )
    admin.projects.add_member(project.project_id, OPERATOR[0], role="editor")
    admin.projects.add_member(project.project_id, VIEWER[0], role="viewer")
    summary["project_id"] = project.project_id
    say(f"  workspace {project.project_id}: admin owns it, operator edits, viewer reads")

    say("== 2. the gateway, as CALIBER sees it")
    status = admin.gateway.get()
    check(status["configured"] is True, "a gateway URI is configured")
    check(status["reachable"] is True, "the gateway answers discovery")
    names = [e["name"] for e in status["endpoints"]]
    check(
        names == ["chat-lab", "embeddings-lab"],
        f"discovery lists the gateway's own endpoints: {names}",
    )
    check(
        status["routing_through_gateway"] is True,
        "CALIBER's own LLM base URL points through the gateway",
    )
    pause_gateway()
    paused = admin.gateway.get()
    check(
        paused["configured"] is True and paused["reachable"] is False and paused["error"],
        "a paused gateway degrades to unreachable-with-error, never a failed page",
    )
    resume_gateway()
    check(admin.gateway.get()["reachable"] is True, "and recovers the moment it answers again")

    say("== 3. the key is wrong, and the gateway's own ledger proves where it failed")
    setup = admin.settings.llm()
    check(setup.llm_provider == "openai", "the platform's LLM provider is openai (via the gateway)")
    check(setup.openai_key_present is True, "a key is present -- presence only, never the value")
    # The stack boots with this wrong key; set it again so a re-run (after a previous run
    # rotated it) stages the same failure deliberately instead of depending on boot state.
    admin.settings.update_llm(openai_api_key="lab20-boot-key-wrong")
    wf = operator.workflows.import_workflow(
        manifest_yaml=workflow_manifest(), name=f"metered-reply{suffix}"
    )
    version = operator.workflows.versions.list(wf.workflow_id)[0]
    operator.workflows.versions.publish(version.version_id)
    summary["workflow_id"] = wf.workflow_id
    refused = operator.workflows.versions.run(version.version_id, input="First try")
    check(
        refused["status"] == "error" and "401" in (refused["error"] or ""),
        "the run errors, and the error names the gateway's 401",
    )
    check(
        gateway_calls(ledger, ok=False) == 1 and gateway_calls(ledger, ok=True) == 0,
        "the gateway's own ledger shows exactly one refused call and no successful one",
    )

    say("== 4. an admin rotates the key at runtime, through the governed surface")
    expect_status(
        403,
        lambda: operator.settings.update_llm(openai_api_key=gateway_key()),
        "an operator cannot write provider credentials",
    )
    admin.settings.update_llm(openai_api_key=gateway_key())
    rotated = admin.settings.llm()
    check(
        (rotated.openai_key_fingerprint or "").endswith(gateway_key()[-4:]),
        "the masked fingerprint names the live key without disclosing it",
    )

    say("== 5. pricing: the operator puts a price on the gateway's model")
    # Pricing rows are global and unique per (provider, model_id), so a re-run adopts the
    # existing row and resets its rates (the admin-only edit) instead of failing on its own history.
    try:
        row = operator.llm_pricing.create(
            provider="lab",
            model_id=model_id,
            prompt_price=PROMPT_PRICE,
            completion_price=COMPLETION_PRICE,
            tags=[f"cookbook-20{suffix}"],
        )
        say(f"  created pricing row {row['pricing_id']}")
    except CaliberAPIError as exc:
        check(exc.status_code == 409, f"an existing row is adopted -> HTTP {exc.status_code}")
        row = next(
            r
            for r in admin.llm_pricing.list(status="active")
            if r["provider"] == "lab" and r["model_id"] == model_id
        )
        admin.llm_pricing.update(
            row["pricing_id"], prompt_price=PROMPT_PRICE, completion_price=COMPLETION_PRICE
        )
        say(f"  adopted pricing row {row['pricing_id']} and reset its rates")
    pricing_id = row["pricing_id"]
    summary["pricing_id"] = pricing_id
    expect_status(
        409,
        lambda: operator.llm_pricing.create(
            provider="lab", model_id=model_id, prompt_price=9.0, completion_price=9.0
        ),
        "a second row for the same provider and model is refused",
    )
    expect_status(
        403,
        lambda: viewer.llm_pricing.create(
            provider="lab", model_id="viewer-model", prompt_price=1.0, completion_price=1.0
        ),
        "a viewer cannot create pricing",
    )
    expect_status(
        403,
        lambda: operator.llm_pricing.update(pricing_id, prompt_price=5.0),
        "updating a rate is admin-only, even for the operator who created it",
    )

    say("== 6. the metered run: tokens become dollars, and two ledgers agree")
    # Usage aggregates every trace on the stack, so the assertions are deltas from here.
    before = by_model_row(admin.gateway.usage(since_ms=0), model_id) or {
        "calls": 0,
        "tokens": 0,
        "cost_usd": 0.0,
    }
    result = operator.workflows.versions.run(version.version_id, input="Hello, gateway")
    check(result["status"] == "completed", "the run completed through the gateway")
    check(
        "metered gateway" in result["output"],
        "the output is the gateway model's reply, not a simulation",
    )
    check(
        gateway_calls(ledger, ok=True) == 1, "the gateway's own ledger shows exactly one real call"
    )
    metered = settled_usage(admin, model_id, min_calls=before["calls"] + 1)
    check(
        metered["calls"] == before["calls"] + 1
        and metered["tokens"] == before["tokens"] + TOKENS_PER_CALL,
        f"one more call, {TOKENS_PER_CALL} more tokens -- the stand-in bills a flat 1000+1000",
    )
    check(
        abs(metered["cost_usd"] - before["cost_usd"] - COST_PER_CALL) < 1e-6,
        f"it cost exactly ${COST_PER_CALL}: "
        f"(1000/1000)x${PROMPT_PRICE} + (1000/1000)x${COMPLETION_PRICE}",
    )

    say("== 7. the meter is written at call time, not at read time")
    admin.llm_pricing.update(pricing_id, prompt_price=10.0)
    after_raise = by_model_row(admin.gateway.usage(since_ms=0), model_id)
    assert after_raise is not None
    check(
        abs(after_raise["cost_usd"] - metered["cost_usd"]) < 1e-6,
        "raising the price does not rewrite what past calls cost",
    )
    operator.workflows.versions.run(version.version_id, input="Second call, new rate")
    final = settled_usage(admin, model_id, min_calls=metered["calls"] + 1)
    new_call_cost = (1000 / 1000) * 10.0 + (1000 / 1000) * COMPLETION_PRICE
    check(
        final["calls"] == metered["calls"] + 1
        and abs(final["cost_usd"] - metered["cost_usd"] - new_call_cost) < 1e-6,
        f"the next call is billed at the new rate: ${new_call_cost}",
    )
    check(gateway_calls(ledger, ok=True) == 2, "the gateway's ledger agrees: two real calls")
    admin.llm_pricing.update(
        pricing_id, prompt_price=PROMPT_PRICE, completion_price=COMPLETION_PRICE
    )

    say("== 8. the secrets store: the governed home for a key like this one")
    secret_name = f"lab20-gateway-key{suffix}"
    expect_status(
        403,
        lambda: operator.secrets.put(secret_name, gateway_key()),
        "writing a secret is admin-only",
    )
    first = admin.secrets.put(secret_name, gateway_key())
    rotatedv = admin.secrets.put(secret_name, gateway_key() + "-v2")
    check(
        int(rotatedv["current_version"]) == int(first["current_version"]) + 1,
        "rotating a secret bumps its version (a rotation, not an overwrite)",
    )
    check("value" not in first and "value" not in rotatedv, "the response never carries the value")
    check(
        rotatedv["reference"] == f"secret://{secret_name}",
        "the reference is the secret:// form other features resolve",
    )
    listed = admin.secrets.list()
    entry = next(s for s in listed["secrets"] if s["name"] == secret_name)
    check("value" not in entry, "the listing carries metadata and a reference, never the value")
    check(
        entry.get("reference") == f"secret://{secret_name}"
        or listed.get("reference_scheme") == "secret://",
        "the reference scheme is secret://, the form the LLM key source accepts at boot",
    )
    admin.secrets.revoke(secret_name)
    summary["secret"] = secret_name

    say("== 9. guardrail definitions, honestly scoped")
    catalog = admin.gateway.guardrail_catalog()
    check(
        catalog["configured"] and catalog["reachable"],
        "the tracking store's guardrail surface is reachable",
    )
    deterministic = {t["type"] for t in catalog["templates"] if t.get("deterministic")}
    check(
        {"regex", "pii"} <= deterministic,
        "the catalog offers deterministic kinds (regex, pii) that need no judge model",
    )
    created = operator.gateway.create_guardrail(
        name=f"no-internal-hosts{suffix}",
        stage="BEFORE",
        action="VALIDATION",
        scorer_type="regex",
        config={"pattern": "internal\\.corp"},
    )
    guardrail_id = created["guardrail_id"]
    listing = admin.gateway.guardrails()
    check(
        any(g["guardrail_id"] == guardrail_id for g in listing.get("guardrails", [])),
        "the guardrail is defined and listed",
    )
    say(
        "  note: definitions live in the MLflow tracking store; they are enforced by MLflow's own "
        "AI Gateway, not by the standalone gateway this lab's Endpoints tab lists"
    )
    admin.gateway.delete_guardrail(guardrail_id)

    say("== 10. the audit trail")
    entries = admin.audit.list(limit=200)
    actions = {e.action for e in entries}
    for action in (
        "settings.update_llm",
        "update_llm_pricing",
        "put_secret",
        "revoke_secret",
        "create_gateway_guardrail",
        "delete_gateway_guardrail",
    ):
        check(action in actions, f"audit log has {action}")
    # create_llm_pricing appears only on a stack where this run created the row
    # (a re-run adopts the existing one), so it is checked permissively.
    if "create_llm_pricing" in actions:
        say("  ok  audit log has create_llm_pricing (this run or an earlier one created the row)")

    say("== 11. the gallery starter")
    recipe = {item.id: item for item in admin.cookbooks.list()}["20"]
    check(len(recipe.steps) > 0, f"the gallery lists {len(recipe.steps)} step(s) for this lab")

    say("== done")
    return summary


def cleanup(admin: CaliberClient, suffix: str = "") -> None:
    for project in admin.projects.list():
        if project.name == f"gateway-ops{suffix}":
            admin.projects.archive(project.project_id)
            say(f"archived workspace {project.project_id}")
    for row in admin.llm_pricing.list(status="active"):
        if f"cookbook-20{suffix}" in (row.get("tags") or []):
            admin.llm_pricing.update(row["pricing_id"], status="archived")
            say(f"archived pricing row {row['pricing_id']}")
    try:
        admin.secrets.delete(f"lab20-gateway-key{suffix}")
        say("deleted the lab secret")
    except CaliberError:
        pass


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB20_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix)
        return
    ensure_accounts(admin)
    result = run(
        admin,
        operator=login_client(base_url, *OPERATOR),
        viewer=login_client(base_url, *VIEWER),
        ledger=ledger_path(),
        suffix=suffix,
    )
    say(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
