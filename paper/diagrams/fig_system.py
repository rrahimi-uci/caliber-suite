"""Figure: high-level architecture and component interaction.

One ASGI process, the clients that reach it, and the systems it integrates with but
does not own. Two properties the layout exists to make visible: the kernel is the
only tier that touches state, and the only path that leaves the platform entirely is
the production agent's round trip along the outer lane.

Routing convention, carried over from the TikZ original: every long edge runs in the
gutter between the process band and the state column, in the outer lane to the right
of everything, or in the channel below the band. No edge crosses a box.

Coordinates are points at final printed size.
"""

from __future__ import annotations

from palette import INK, role
from scene import SIZE_BODY, SIZE_HEAD, Scene
from stats import stat

TARGET_WIDTH_PT = 494.0

# ------------------------------------------------------------------ geometry ---
CLW = 78.0     # clients column
MX = 90.0      # process band left edge
MW = 262.0     # process band width
RX = 366.0     # state column left edge
RW = 128.0     # state column width
GA = RX - 8.0  # gutter lane: control plane out to MLflow
GB = 490.0     # outer lane: the agent's round trip
PAD = 6.0      # inset of a row inside the band
ROWW = MW - 2 * PAD


def build() -> Scene:
    s = Scene("fig-system", width=TARGET_WIDTH_PT, height=0)
    y = 14.0

    # The band is drawn first so every row sits on top of it; its height is
    # corrected once the rows have placed themselves.
    band = s.band(MX, y - 4, MW, 10)
    s.text(MX + PAD, 2, "ONE CALIBER ASGI APPLICATION", size=SIZE_BODY,
           bold=True, align="left", colour=role("muted").stroke)

    def row(title, detail, r, *, height=0.0, width=ROWW, x=None):
        nonlocal y
        b = s.labelled_box(MX + PAD if x is None else x, y, width, title, detail,
                           role_name=r, title_size=SIZE_BODY,
                           detail_size=SIZE_BODY, min_height=height)
        y = b["y"] + b["height"] + 7
        return b

    adm = row("Admission", "session or token, scopes, CSRF, rate limit", "govern")

    # ---- surfaces -----------------------------------------------------------
    surf = [("Route handlers", f"{stat('statRouteDecls')} declarations"),
            ("SSE + webhooks", "live events"),
            ("Aria tool loop", "risk-tiered")]
    w = (ROWW - 2 * 5) / 3
    tops = []
    for i, (t, d) in enumerate(surf):
        tops.append(s.labelled_box(MX + PAD + i * (w + 5), y, w, t, d,
                                   role_name="surface", title_size=SIZE_BODY,
                                   detail_size=SIZE_BODY))
    y = max(b["y"] + b["height"] for b in tops) + 7

    modes = row("Author > Test > Evaluate > Calibrate > Release > Observe", "",
                "control")

    # ---- control-plane services --------------------------------------------
    svc = [("Orchestrator", "triage, diagnose, candidate"),
           ("Evaluation", "scorecards, LLM judges"),
           ("Calibration", f"{stat('statOptimizers')} optimizer paths"),
           ("Apply + promote", "gate, alias, checkpoint")]
    w = (ROWW - 3 * 5) / 4
    cells = []
    for i, (t, d) in enumerate(svc):
        cells.append(s.labelled_box(MX + PAD + i * (w + 5), y, w, t, d,
                                    role_name="control", title_size=SIZE_BODY,
                                    detail_size=SIZE_BODY))
    svc_mid = cells[0]["y"] + cells[0]["height"] / 2
    y = max(b["y"] + b["height"] for b in cells) + 7

    gov = row("execution policy, gate verdicts, release control, audit log", "",
              "govern")
    kern = row("config, persistence, storage, tool execution, providers", "",
               "control")

    # ---- durable queues and the loops --------------------------------------
    qw = 74.0
    q = s.labelled_box(MX + PAD, y, qw, "Durable queues",
                       "status, claim and lease columns on rows",
                       role_name="store", title_size=SIZE_BODY,
                       detail_size=SIZE_BODY)
    loops = s.labelled_box(
        MX + PAD + qw + 8, y, ROWW - qw - 8,
        f"Up to {stat('statLoops')} in-process loops",
        "no separate worker tier\n"
        "Refinement, CalibrationDrain, WorkflowRun,\n"
        "AriaPlan, KnowledgeBase, Scheduler,\n"
        "Janitor, WebhookDispatcher",
        role_name="async", title_size=SIZE_BODY, detail_size=SIZE_BODY,
        min_height=q["height"],
    )
    # The atomic claim, and the status transition written back.
    qy = q["y"] + q["height"] / 2
    s.arrow([(q["x"] + q["width"] + 1, qy - 4), (loops["x"] - 1, qy - 4)],
            colour=role("async").stroke)
    s.arrow([(loops["x"] - 1, qy + 4), (q["x"] + q["width"] + 1, qy + 4)],
            colour=role("async").stroke)
    band_bottom = max(q["y"] + q["height"], loops["y"] + loops["height"]) + 6
    band["height"] = round(band_bottom - band["y"], 2)

    # ======================= clients ==========================================
    s.text(0, 2, "CLIENTS", size=SIZE_BODY, bold=True, align="left",
           colour=role("muted").stroke)
    cy = 14.0
    for t, d in [("Browser", "React SPA session"),
                 ("API client", "account-scoped"),
                 ("Service caller", "published workflow")]:
        b = s.labelled_box(0, cy, CLW, t, d, role_name="surface",
                           title_size=SIZE_BODY, detail_size=SIZE_BODY)
        s.arrow([(CLW + 1, b["y"] + b["height"] / 2),
                 (MX + PAD - 1, adm["y"] + adm["height"] / 2)])
        cy = b["y"] + b["height"] + 6
    agent = s.labelled_box(0, cy + 8, CLW, "Production agent",
                           "the consumer, outside the control plane",
                           role_name="extern", title_size=SIZE_BODY,
                           detail_size=SIZE_BODY)

    # ======================= state and external systems =======================
    s.text(RX, 2, "STATE & EXTERNAL", size=SIZE_BODY, bold=True, align="left",
           colour=role("muted").stroke)
    sy = 14.0
    stores = {}
    for t, d, r in [
        ("MLflow 3.14+", "prompt versions and aliases, traces, assessments", "extern"),
        ("PostgreSQL 17", f"{stat('statModels')} tables: metadata and the file inventory", "store"),
        ("Object storage", "file bytes only", "store"),
        ("LLM providers", "OpenAI, Claude, Gateway", "extern"),
        ("MCP servers", "containment or sidecar", "extern"),
        ("Event transport", "NATS, Redis, DB", "async"),
    ]:
        b = s.labelled_box(RX, sy, RW, t, d, role_name=r,
                           title_size=SIZE_BODY, detail_size=SIZE_BODY)
        stores[t] = b
        sy = b["y"] + b["height"] + 6

    # Only the kernel touches state.
    kern_right = (MX + MW - PAD, kern["y"] + kern["height"] / 2)
    for name in ("PostgreSQL 17", "Object storage", "LLM providers", "MCP servers"):
        b = stores[name]
        s.arrow([kern_right, (b["x"] - 1, b["y"] + b["height"] / 2)])
    s.arrow([(loops["x"] + loops["width"], loops["y"] + 6),
             (stores["Event transport"]["x"] - 1,
              stores["Event transport"]["y"] + stores["Event transport"]["height"] / 2)],
            colour=role("async").stroke)

    # The gutter lane: registry and evaluate effects.
    mlf = stores["MLflow 3.14+"]
    s.arrow([(MX + MW - 2, svc_mid), (GA, svc_mid), (GA, mlf["y"] + 8),
             (RX - 1, mlf["y"] + 8)])

    # ======================= the outer lane ===================================
    ch = band_bottom + 22
    s.arrow(
        [(CLW / 2, agent["y"] + agent["height"] + 1), (CLW / 2, ch),
         (GB, ch), (GB, mlf["y"] + 8), (RX + RW + 1, mlf["y"] + 8)],
        colour=role("extern").stroke, both=True,
    )
    s.text(
        (CLW / 2 + GB) / 2, ch - 20,
        "the agent resolves @prod at call time and emits traces: when Release\n"
        "rotates the alias, the next call runs the new version, no client redeploy",
        size=SIZE_BODY, colour=INK,
    )

    s.fit_height()
    return s
