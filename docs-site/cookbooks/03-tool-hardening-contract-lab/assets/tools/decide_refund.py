"""Deterministic refund-eligibility decision for a workflow ``python_code`` node.

This is NOT a registered tool. Paste the body of this module into a CALIBER
**Python Code** node (``Compose → Workflows`` → drag a *Python Code* node). It
needs no registration, versions with the workflow, and uses **stdlib only**.
Wire it as the FIRST node after START: ``START -> decide_refund``. A python_code
node receives the run input as an UNPARSED string in ``run_input``, so this
entrypoint parses it as JSON (when it looks like a JSON object) and reads the
decision fields from that dict. (``json`` is pre-injected into the node sandbox
-- do NOT add a module-level ``import json``.)

Run-input fields (parsed from the run input JSON object):
    order_state       str   -- order lifecycle, e.g. "delivered" | "shipped" |
                               "cancelled" | "returned" | "" (unknown).
    risk_flags        list  -- fraud/risk signals, e.g. ["fraud_suspected"],
                               ["chargeback_history"], or [] (none). May be
                               ``None`` when the risk lookup failed -> we
                               FAIL CLOSED to manual_review (see RULES below).
    amount            float -- refund amount in USD (>= 0).
    days_since_order  int   -- whole days between the order date and now.

Node outputs:
    decision          str   -- one of: "approve" | "deny" | "manual_review".
    reason_code       str   -- stable machine code (see REASON_CODES); pairs
                               with the policy-reason-normalizer skill.
    requires_approval bool  -- True when a human_approval gate must clear before
                               initiate_refund runs. Always True for
                               manual_review; never True for a clean approve.

Deterministic rules (evaluated top-to-bottom; first match wins):
    1. FAIL CLOSED: risk data missing (risk_flags is None) OR order_state is
       empty/unknown -> manual_review / requires_approval=True.
    2. Any fraud/risk flag present -> manual_review / requires_approval=True.
    3. Order not in a refundable state (only "delivered"/"shipped"/"returned"
       are refundable) -> deny.
    4. Outside the refund window (days_since_order > REFUND_WINDOW_DAYS) -> deny.
    5. Negative/zero amount -> deny (nothing to refund).
    6. Amount over the auto-approve threshold (amount > AUTO_APPROVE_MAX_USD)
       -> manual_review / requires_approval=True.
    7. Otherwise (in window, no risk, small amount) -> approve /
       requires_approval=False.

The rule_checks in verification.yaml
(``deterministic_decision_preserved`` / ``approval_required_for_high_risk``)
are enforced HERE plus by the downstream human_approval gate -- not by a judge.
"""

# --- Policy constants (the only knobs; keep them visible at the top) ---------
REFUND_WINDOW_DAYS = 30          # matches demo_tools.lookup_policy
AUTO_APPROVE_MAX_USD = 200.0     # over this -> human approval required
REFUNDABLE_STATES = {"delivered", "shipped", "returned"}

# Stable reason codes (machine-facing; the normalizer skill maps these to copy)
REASON_CODES = {
    "MISSING_RISK_DATA": "Risk signals unavailable; failing closed to review.",
    "UNKNOWN_ORDER_STATE": "Order state unknown; failing closed to review.",
    "RISK_FLAG_PRESENT": "Risk/fraud flag on the account; routing to review.",
    "NOT_REFUNDABLE_STATE": "Order is not in a refundable state.",
    "OUTSIDE_WINDOW": "Past the refund window.",
    "INVALID_AMOUNT": "Refund amount is zero or negative.",
    "AMOUNT_OVER_THRESHOLD": "Amount exceeds the auto-approve limit; needs review.",
    "ELIGIBLE_AUTO_APPROVE": "In window, no risk, within auto-approve limit.",
}


def decide_refund(
    order_state: str,
    risk_flags,
    amount,
    days_since_order,
) -> dict:
    """Pure, deterministic refund decision. Returns the node-output dict."""

    def out(decision: str, reason_code: str, requires_approval: bool) -> dict:
        return {
            "decision": decision,
            "reason_code": reason_code,
            "requires_approval": bool(requires_approval),
        }

    # Rule 1 -- fail closed when inputs are missing/unusable.
    if risk_flags is None:
        return out("manual_review", "MISSING_RISK_DATA", True)
    state = (order_state or "").strip().lower()
    if not state:
        return out("manual_review", "UNKNOWN_ORDER_STATE", True)

    # Coerce numerics defensively (still fail closed on garbage).
    try:
        amount_val = float(amount)
        days = int(days_since_order)
    except (TypeError, ValueError):
        return out("manual_review", "MISSING_RISK_DATA", True)

    # Rule 2 -- any risk flag -> review + approval.
    if any(str(flag).strip() for flag in risk_flags):
        return out("manual_review", "RISK_FLAG_PRESENT", True)

    # Rule 3 -- non-refundable lifecycle state.
    if state not in REFUNDABLE_STATES:
        return out("deny", "NOT_REFUNDABLE_STATE", False)

    # Rule 4 -- outside the refund window.
    if days > REFUND_WINDOW_DAYS:
        return out("deny", "OUTSIDE_WINDOW", False)

    # Rule 5 -- nothing to refund.
    if amount_val <= 0:
        return out("deny", "INVALID_AMOUNT", False)

    # Rule 6 -- large refund needs a human.
    if amount_val > AUTO_APPROVE_MAX_USD:
        return out("manual_review", "AMOUNT_OVER_THRESHOLD", True)

    # Rule 7 -- clean auto-approve.
    return out("approve", "ELIGIBLE_AUTO_APPROVE", False)


# --- python_code node entrypoint --------------------------------------------
# A CALIBER Python Code node calls ``run_python_node(...)`` and uses its RETURN
# value as the node output. A module-level ``result = ...`` is DISCARDED (the
# runtime wraps a body lacking this def in a function with no return), so define
# the entrypoint explicitly and return both output ports.
#
# ``json`` is PRE-INJECTED into the node sandbox; do NOT ``import json`` here.
# Because decide_refund is the first node after START, its inputs arrive as the
# run input -- an UNPARSED string in ``run_input``. Parse it as JSON when it
# looks like a JSON object, then read the decision fields from that dict.


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    if isinstance(run_input, str) and run_input.strip().startswith("{"):
        data = json.loads(run_input)
    else:
        data = run_input or {}
    if not isinstance(data, dict):
        data = {}
    result = decide_refund(
        order_state=data.get("order_state", ""),
        risk_flags=data.get("risk_flags"),  # absent -> None -> fail closed
        amount=data.get("amount", 0),
        days_since_order=data.get("days_since_order", 0),
    )
    return {"text": json.dumps(result), "result": result}
