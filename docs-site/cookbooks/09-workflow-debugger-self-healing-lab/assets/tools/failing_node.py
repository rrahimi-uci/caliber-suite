"""failing_node — Caliber workflow `python_code` node body (deliberate fault).

This is NOT a registered tool. Paste the body of this module into a CALIBER
**Python Code** node (`Compose → Workflows` → drag a *Python Code* node). It
needs no registration, versions with the workflow, and uses **stdlib only**.

Its purpose is to MANUFACTURE a reproducible, code-level node failure for the
workflow-debugger demo (the second failure option in this scenario's README):
on a malformed input it raises, so the run lands in `failed` with a real node
exception you can localize in the Debugger panel.

CONTRACT (how the sandbox calls this)
  The node body runs inside the runtime wrapper:
      run_python_node(input=None, context=None, inputs=None, run_input='')
  Wire the upstream port so this node receives:
      inputs["payload"]  -> a dict; MUST contain a non-empty "account_id"
                            (str) and an "amount" (number >= 0).
  It also tolerates the payload arriving as the single `input` dict (or a JSON
  string) so the node still works when the upstream emits one object.

THE FAULT (what triggers it) — before the patch
  Pre-patch, the body still validates the payload with `.get(...)` (it never
  indexes a key directly), collects every problem into an `errors` list, and
  when `STRICT = True` raises a single composed `ValueError` joining those
  reasons. So ANY of these inputs reproduces a node-level exception (-> `failed`):
    * "account_id" missing entirely        -> ValueError (missing/empty id)
    * "account_id" present but empty ""     -> ValueError (missing/empty id)
    * "amount" non-numeric, e.g. "N/A"      -> ValueError (bad amount)
  The raised message is "failing_node: invalid input -> " + "; ".join(errors).
  In the Debugger step trace this node shows the raised error on its `error`
  marker; that exception text + this node id IS the concrete evidence the
  diagnosis-summary prompt and RootCauseQuality judge must cite.

THE FIX (fixed behavior) — after the manual patch
  The patch is a MANUAL editor edit (save a new workflow version): swap the
  raising lookups for validated, defaulted access and emit a structured
  `validation_error` on the result port instead of throwing. The dividing line
  below — `STRICT = True` — is the smallest change that flips fault->handled.
  Flip it to `False` (or delete the raising branch) and re-run the SAME failing
  input to confirm the node now completes with `status="rejected_input"`
  instead of crashing the run. There is no propose_workflow_patch tool; this
  edit is the patch.

OUTPUTS (on the node's `result` / `text` ports), post-fix
  {
    "status":   "ok" | "rejected_input",
    "account_id": <validated id> | None,
    "amount":     <float>        | None,
    "errors":     [<human-readable reasons the input was rejected>]
  }
"""

# `json` is pre-injected into the node namespace by the workflow Python
# sandbox, so we deliberately do NOT `import json` here — the sandbox's
# SAFE_BUILTINS has no `__import__`, and an import would crash the node.

# The one knob the manual patch flips. True = original deliberately-faulty
# behavior (raise on malformed input -> reproducible node exception).
# Set to False as the minimal scoped fix (validate + return, never throw).
STRICT = True


def _coerce_payload(value):
    """Accept a dict directly or a JSON string; anything else -> {}."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _validate(payload):
    """Return (account_id, amount, errors) without raising."""
    errors = []

    account_id = payload.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        errors.append("missing or empty required field 'account_id'")
        account_id = None
    else:
        account_id = account_id.strip()

    raw_amount = payload.get("amount")
    amount = None
    try:
        amount = float(raw_amount)
        if amount < 0:
            errors.append("field 'amount' must be >= 0")
            amount = None
    except Exception:
        errors.append("field 'amount' is missing or not a number")

    return account_id, amount, errors


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    src = inputs if isinstance(inputs, dict) else {}

    # Resolve the payload from inputs["payload"], else the single input object.
    payload = _coerce_payload(src.get("payload"))
    if not payload:
        payload = _coerce_payload(input if input is not None else context)

    account_id, amount, errors = _validate(payload)

    if STRICT and errors:
        # --- DELIBERATE FAULT (pre-patch) -----------------------------------
        # Reproduces a real node-level exception so the run lands in `failed`.
        # The first failing reason becomes the raised message — exactly the
        # evidence the debugger surfaces and the diagnosis must cite.
        raise ValueError(
            "failing_node: invalid input -> {}".format("; ".join(errors))
        )

    # --- FIXED BEHAVIOR (post-patch: STRICT=False) --------------------------
    if errors:
        result = {
            "status": "rejected_input",
            "account_id": account_id,
            "amount": amount,
            "errors": errors,
        }
    else:
        result = {
            "status": "ok",
            "account_id": account_id,
            "amount": amount,
            "errors": [],
        }
    return {"text": json.dumps(result), "result": result}
