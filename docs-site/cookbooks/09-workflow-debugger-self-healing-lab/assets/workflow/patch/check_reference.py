ORDER_REF = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*-?$")

def run_python_node(input=None, context=None, inputs=None, run_input=''):
    ticket = dict(inputs.get("ticket") or {})
    ref = str(ticket.get("order_ref") or "")
    ticket["reference_valid"] = len(ref) <= 64 and bool(ORDER_REF.match(ref))
    ticket["query"] = {"account_id": ticket["account_id"]} if ticket.get("ok") else {}
    return ticket
