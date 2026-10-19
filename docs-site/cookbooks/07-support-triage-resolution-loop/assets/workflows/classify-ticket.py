# Body of the `classify` Python Code node in support-triage.workflow.json.
#
# The node runs in CALIBER's tool sandbox: no `import` statements (json, re, math, datetime and
# statistics are already in scope), a restricted set of builtins, no private/dunder attributes.
# It reads one ticket (a JSON string, or plain text) and returns the triage record the router
# and the verifier read. Rules run top to bottom and the first match wins, so the order below
# IS the policy; keep it in step with the table in the lab README.


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    raw = input or run_input or ""
    ticket = {}
    try:
        parsed = json.loads(raw)
        ticket = parsed if isinstance(parsed, dict) else {"ticket_text": str(raw)}
    except ValueError:
        ticket = {"ticket_text": str(raw)}

    text = str(ticket.get("ticket_text") or "").strip()
    low = text.lower()
    tier = str(ticket.get("customer_tier") or "free").lower()
    if tier not in ("free", "pro", "enterprise"):
        tier = "free"
    account_id = str(ticket.get("account_id") or "").strip()
    duplicate_of = str(ticket.get("duplicate_of") or "").strip()

    def has(words):
        return any(w in low for w in words)

    injection = has(
        [
            "ignore your instructions",
            "ignore all previous",
            "ignore previous instructions",
            "reveal your system prompt",
            "you are now in",
        ]
    )
    abusive = has(
        ["idiot", "stupid", "garbage", "worthless", "useless", "pathetic", "moron", "hate you"]
    )
    card = re.search(r"\b(?:\d[ -]?){13,16}\b", text) is not None
    ssn = re.search(r"\b\d{3}-\d{2}-\d{4}\b", text) is not None
    secret = has(["password is", "api key is", "sk_live_", "sk_test_", "private key"])
    pii = card or ssn or secret
    legal = has(
        [
            "gdpr",
            "delete all personal data",
            "right to be forgotten",
            "subpoena",
            "lawyer",
            "legal action",
            "data breach",
        ]
    )
    data_loss = has(["data loss", "lost data", "we lost", "disappeared"])
    bug = data_loss or has(
        [
            "500",
            "502",
            "503",
            "504",
            "error code",
            "stack trace",
            "traceback",
            "exception",
            "crash",
            "regression",
            "stuck",
            "not loading",
            "timeout",
            "timed out",
            "outage",
            "is down",
            "cannot log in",
            "can't log in",
            "misaligned",
            "fails with",
        ]
    )
    billing = has(
        [
            "charged twice",
            "double charged",
            "was charged",
            "were charged",
            "unauthorized charge",
            "chargeback",
            "overcharged",
            "need a refund",
            "want a refund",
            "please refund",
            "refund the",
            "refund my",
        ]
    )
    how_to = has(
        [
            "how do i",
            "how can i",
            "how to",
            "where do i",
            "where can i",
            "is it possible to",
            "does caliber support",
        ]
    )

    flags = []
    severity = "low"
    decision = "clarify"
    approval = False

    if not text:
        flags.append("empty_ticket")
    elif injection:
        flags.append("prompt_injection")
    elif abusive:
        decision, severity = "escalate_support", "medium"
        flags.append("abusive")
    elif pii:
        decision, severity = "escalate_support", "high"
        flags.append("pii_exposed")
    elif legal:
        decision, severity = "escalate_support", "high"
        flags.append("legal_or_privacy")
    elif duplicate_of:
        decision = "reply"
        flags.append("duplicate")
    elif bug:
        decision, approval = "escalate_bug", True
        urgent_markers = has(["production", "outage", "is down", "all users", "blocking", "whole team"])
        if data_loss or (urgent_markers and tier == "enterprise"):
            severity = "urgent"
        elif urgent_markers:
            severity = "high"
        elif has(["cosmetic", "minor"]):
            severity = "low"
        else:
            severity = "medium"
        if billing and account_id:
            flags.append("refund_request")
    elif billing:
        if account_id:
            decision, approval = "escalate_support", True
            severity = "high" if has(["unauthorized", "fraud"]) or tier == "enterprise" else "medium"
            flags.append("refund_request")
        else:
            flags.append("missing_account_id")
    elif how_to:
        decision = "reply"

    sla_table = {
        "enterprise": {"urgent": 15, "high": 60, "medium": 240, "low": 720},
        "pro": {"urgent": 30, "high": 120, "medium": 480, "low": 1440},
        "free": {"urgent": 120, "high": 480, "medium": 1440, "low": 4320},
    }
    result = {
        "decision": decision,
        "severity": severity,
        "sla_minutes": sla_table[tier][severity],
        "requires_approval": approval,
        "flags": sorted(flags),
        "customer_tier": tier,
        "channel": str(ticket.get("channel") or ""),
        "account_id": account_id,
        "duplicate_of": duplicate_of,
        "ticket_text": text,
    }
    return {
        "text": json.dumps(result),
        "result": result,
        "decision": decision,
        "approval": "true" if approval else "false",
    }
