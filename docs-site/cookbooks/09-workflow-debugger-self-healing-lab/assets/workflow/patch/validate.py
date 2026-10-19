CURRENCIES = {"USD": "USD", "EUR": "EUR", "GBP": "GBP", "$": "USD", "\u20ac": "EUR", "\u00a3": "GBP"}

def _amount(value):
    if isinstance(value, bool):
        return None, "amount must be a number, got " + repr(value)
    if isinstance(value, (int, float)):
        return value, None
    if isinstance(value, str):
        text = value.strip()
        for symbol in ("$", "\u20ac", "\u00a3", "USD", "EUR", "GBP"):
            text = text.replace(symbol, "")
        text = text.strip()
        if re.match(r"^[0-9]+,[0-9]{1,2}$", text):
            return None, "amount " + repr(value) + " is ambiguous: comma may be a decimal separator"
        text = text.replace(",", "")
        if re.match(r"^[0-9]+(\.[0-9]+)?$", text):
            return (float(text) if "." in text else int(text)), None
    return None, "amount must be a number, got " + repr(value)

def run_python_node(input=None, context=None, inputs=None, run_input=''):
    raw = input if input is not None else run_input
    errors = []
    try:
        ticket = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"ok": False, "errors": ["ticket is not valid JSON"]}
    if not isinstance(ticket, dict):
        return {"ok": False, "errors": ["ticket must be a JSON object"]}
    for key in ("ticket_id", "account_id", "amount", "currency", "reason", "order_ref"):
        if key not in ticket:
            errors.append("missing required field '" + key + "'")
    out = {"ticket_id": ticket.get("ticket_id")}
    if "account_id" in ticket:
        account_id = str(ticket["account_id"]).strip().upper()
        if not re.match(r"^A-[0-9]{4}$", account_id):
            errors.append("account_id must look like A-1234, got " + repr(ticket["account_id"]))
        out["account_id"] = account_id
    if "amount" in ticket:
        amount, problem = _amount(ticket["amount"])
        if problem:
            errors.append(problem)
        elif amount <= 0:
            errors.append("amount must be greater than 0, got " + repr(ticket["amount"]))
        out["amount"] = amount
    if "currency" in ticket:
        currency = CURRENCIES.get(str(ticket["currency"]).strip().upper()) or CURRENCIES.get(str(ticket["currency"]).strip())
        if currency is None:
            errors.append("unsupported currency " + repr(ticket["currency"]))
        out["currency"] = currency
    if "reason" in ticket:
        if not str(ticket["reason"]).strip():
            errors.append("reason must not be empty")
        out["reason"] = str(ticket["reason"]).strip()
    if "order_ref" in ticket:
        out["order_ref"] = str(ticket["order_ref"]).strip()
    out["ok"] = not errors
    if errors:
        out["errors"] = errors
    return out
