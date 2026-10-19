LIMITS = {"standard": 200, "premium": 500}

def run_python_node(input=None, context=None, inputs=None, run_input=''):
    ticket = inputs["ticket"]
    if not ticket.get("ok"):
        return {
            "ticket_id": ticket.get("ticket_id"),
            "decision": "rejected_input",
            "reason_code": "INVALID_TICKET",
            "errors": ticket.get("errors", []),
        }
    response = inputs["account"]
    account = response.get("json")
    if response.get("status_code") != 200 or not isinstance(account, dict) or account.get("tier") not in LIMITS:
        return {
            "ticket_id": ticket["ticket_id"],
            "decision": "manual_review",
            "reason_code": "ACCOUNT_LOOKUP_FAILED",
            "upstream_status": response.get("status_code"),
            "amount": ticket["amount"],
            "currency": ticket["currency"],
        }
    if account.get("status") != "active":
        decision, reason = "manual_review", "ACCOUNT_NOT_ACTIVE"
    elif ticket["amount"] > LIMITS[account["tier"]]:
        decision, reason = "manual_review", "AMOUNT_OVER_LIMIT"
    elif not ticket["reference_valid"]:
        decision, reason = "manual_review", "ORDER_REF_UNVERIFIED"
    else:
        decision, reason = "approve", "ELIGIBLE_AUTO_APPROVE"
    return {
        "ticket_id": ticket["ticket_id"],
        "decision": decision,
        "reason_code": reason,
        "amount": ticket["amount"],
        "currency": ticket["currency"],
    }
