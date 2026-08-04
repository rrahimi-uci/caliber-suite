"""Figure 1: the six-layer stack.

Read bottom-up: infrastructure carries kernel services; kernel services obey a
governance substrate; the substrate governs asset families (the nouns); lifecycle
modes (the verbs) act on those nouns; surfaces expose the result. The dashed left
rail holds platform services that cut across all six layers rather than occupying
one, and its dashed outline is what marks that permeability.

All coordinates are points at final printed size, so the figure is placed at
``width=494pt`` with no scaling and its 7pt detail text really is 7pt.

Mode names are title case rather than full caps: "CALIBRATE" in bold at 8pt is
47pt wide and does not fit a sixth of the content strip, whereas "Calibrate" is
33pt. The TikZ original used small caps, which are narrower than the full caps
Latin Modern Sans gives here.
"""

from __future__ import annotations

from palette import INK, role
from scene import SIZE_BODY, SIZE_HEAD, Scene
from stats import stat

TARGET_WIDTH_PT = 494.0  # 17.4cm: full width with the article layout's overhang

# ------------------------------------------------------------------ geometry ---
RAIL = 66.0        # cross-cutting rail
CX = 74.0          # content strip left edge
CW = 342.0         # content strip width
GAP = 5.0          # gutter between boxes
LX = 424.0         # layer-label column left edge
LW = 70.0          # layer-label column width
BAND_H = 30.0      # every band is the same height so the stack reads as a stack
SPINE = 10.0       # vertical space the "rests on" arrow occupies

BANDS = [
    ("surface", "6 \u00b7 SURFACES", "the interface", [
        ("React SPA", "same origin"),
        ("HTTP API", f"{stat('statRouteDecls')} routes"),
        ("Aria copilot", "tool loop"),
        ("Headless", "SSE, webhooks"),
    ]),
    ("control", "5 \u00b7 LIFECYCLE MODES", "the verbs", [
        ("Author", "draft, validate"),
        ("Test", "bounded runs"),
        ("Evaluate", "score, judge"),
        ("Calibrate", "optimize"),
        ("Release", "gate, promote"),
        ("Observe", "trace, meter"),
    ]),
    ("asset", "4 \u00b7 ASSET FAMILIES", "the nouns", [
        ("Prompt", ""), ("Tool", ""), ("Skill", ""), ("MCP", ""),
        ("Work-\nflow", ""), ("Know.\nbase", ""), ("Test\nset", ""),
        ("Judge", ""), ("Agent", ""),
    ]),
    ("govern", "3 \u00b7 GOVERNANCE", "the rules", [
        ("Identity", f"{stat('statScopes')} scopes"),
        ("Exec. policy", "egress"),
        ("Evidence", "verdicts"),
        ("Release", "promote"),
        ("Ledgers", "audit"),
    ]),
    ("control", "2 \u00b7 KERNEL", "services", [
        ("Config", ""), ("Persis-\ntence", ""), ("Storage", ""),
        ("Tool\nexec.", ""), ("Event\nbus", ""), ("Observ-\nability", ""),
        ("Provider\nadapters", ""),
    ]),
    (None, "1 \u00b7 INFRASTRUCTURE", "the base", [
        ("Starlette", "ASGI host"),
        ("Postgres 17", "pgvector, AGE"),
        ("Object store", "S3, local"),
        ("MLflow 3.14+", "registry"),
        ("Transport", "NATS, Redis"),
    ]),
]

# The bottom band mixes roles: three durable stores, one external system, one
# asynchronous transport. Naming them per cell rather than per band is the point.
INFRA_ROLES = ["store", "store", "store", "extern", "async"]

PLATFORM_SERVICES = [
    "Evidence base", "Evaluation", "Calibration",
    "Capability\nregistry", "Integration hub", "Project scoping",
]


def build() -> Scene:
    total_h = len(BANDS) * BAND_H + (len(BANDS) - 1) * SPINE
    s = Scene("fig-layers", width=TARGET_WIDTH_PT, height=total_h)

    for band_i, (band_role, label, gloss, cells) in enumerate(BANDS):
        y = band_i * (BAND_H + SPINE)
        n = len(cells)
        w = (CW - (n - 1) * GAP) / n
        for i, (title, detail) in enumerate(cells):
            x = CX + i * (w + GAP)
            r = INFRA_ROLES[i] if band_role is None else band_role
            box = s.box(x, y, w, BAND_H, role_name=r)
            # A nine-cell band leaves 34pt per cell: the family names go in at body
            # size with no detail line, which is why that band carries no detail.
            head = SIZE_BODY if n >= 7 else SIZE_HEAD
            s.label_in(box, title, detail, title_size=head)

        # The "rests on" spine: each band hands off to the one below it.
        if band_i < len(BANDS) - 1:
            mid = CX + CW / 2
            s.arrow([(mid, y + BAND_H + 1.5), (mid, y + BAND_H + SPINE - 1.5)],
                    colour=role("muted").stroke)

        # Layer label, in the right-hand column, wrapped to the column width so a
        # long name like INFRASTRUCTURE breaks at a space instead of overrunning.
        wrapped = s.wrap(label, LW, SIZE_BODY, bold=True)
        s.text(LX, y + 3, wrapped, size=SIZE_BODY, bold=True, align="left",
               width=LW)
        s.text(LX, y + 3 + SIZE_BODY * 1.28 * len(wrapped.split("\n")), gloss,
               size=SIZE_BODY, italic=True, align="left", width=LW,
               colour=role("muted").stroke)

    # ---- the cross-cutting rail ----------------------------------------------
    # Dashed, because these services are not a layer: every layer reaches them.
    s.band(0, 0, RAIL, total_h, dashed=True)
    s.text(5, 5, "PLATFORM\nSERVICES", size=SIZE_BODY, bold=True, align="left")
    s.text(5, 5 + 2 * SIZE_BODY * 1.28 + 3,
           "not a layer:\nevery layer\nreaches them",
           size=SIZE_BODY, italic=True, align="left",
           colour=role("muted").stroke)
    y = 5 + 5 * SIZE_BODY * 1.28 + 12
    for item in PLATFORM_SERVICES:
        s.text(5, y, item, size=SIZE_BODY, align="left", colour=INK)
        y += len(item.split("\n")) * SIZE_BODY * 1.28 + 4

    return s
