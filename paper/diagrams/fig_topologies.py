"""Figure: the two deployment topologies.

One ASGI application, two ways to run it. The API and the SPA are identical, so the
choice is a failure-domain and operations decision rather than a feature one -- and
the layout is built to make that the obvious reading, with the same SPA feeding both
bands and the same pair of store rows beneath each.

The bottom note exists to defuse a specific, expensive confusion: two different
stores are both called "the artifact store", and support on one is not support on
the other.
"""

from __future__ import annotations

from palette import INK, role
from scene import SIZE_BODY, SIZE_HEAD, SIZE_SMALL, Scene

TARGET_WIDTH_PT = 340.0  # 12cm: a portrait figure, scaled up by the layout

W = TARGET_WIDTH_PT
HW = 164.0        # half-column width
LX = 0.0          # left band
RXX = W - HW      # right band


def build() -> Scene:
    s = Scene("fig-topologies", width=W, height=0)

    # ---- the shared surface --------------------------------------------------
    spa = s.labelled_box(W / 2 - 70, 0, 140, "React SPA", "same-origin /caliber/",
                         role_name="surface", title_size=SIZE_BODY,
                         detail_size=SIZE_BODY)
    s.text(W / 2, spa["height"] + 3, "identical in both topologies: pick one",
           size=SIZE_SMALL, italic=True, colour=role("muted").stroke)

    # ---- topology A: embedded ----------------------------------------------
    # Each box sizes itself, then the band is fitted around what the boxes turned
    # out to be. Fixing the band first and hoping the contents fit is how the
    # first version of this figure overflowed.
    BAND_TOP = 58.0  # leaves a visible shaft on the arrows from the SPA
    a = s.labelled_box(
        LX + 5, BAND_TOP + 7, HW - 10, "One MLflow server process",
        "MLflow core AND the CALIBER ASGI\napp, mounted as mlflow.app\n\n"
        "shared failure domain: one crash\ntakes both surfaces down",
        role_name="govern", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )

    # ---- topology B: standalone --------------------------------------------
    b1 = s.labelled_box(
        RXX + 5, BAND_TOP + 7, HW - 10, "CALIBER ASGI :5001",
        "API, SPA, in-process loops",
        role_name="govern", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )
    b2 = s.labelled_box(
        RXX + 5, b1["y"] + b1["height"] + 12, HW - 10, "MLflow :5000",
        "traces, registry, evaluate",
        role_name="extern", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )
    s.arrow([(RXX + HW / 2, b1["y"] + b1["height"] + 1.5),
             (RXX + HW / 2, b2["y"] - 1.5)])
    s.text(RXX + HW / 2 + 20, b1["y"] + b1["height"] + 1, "HTTP",
           size=SIZE_SMALL, colour=role("muted").stroke)

    band_h = max(
        a["y"] + a["height"], b2["y"] + b2["height"]
    ) - BAND_TOP + 7
    s.band(LX, BAND_TOP, HW, band_h)
    s.band(RXX, BAND_TOP, HW, band_h)
    s.text(LX + 3, BAND_TOP - 11, "A \u00b7 EMBEDDED", size=SIZE_BODY, bold=True,
           align="left", colour=role("muted").stroke)
    s.text(RXX + 3, BAND_TOP - 11, "B \u00b7 STANDALONE", size=SIZE_BODY,
           bold=True, align="left", colour=role("muted").stroke)
    band_bottom = BAND_TOP + band_h

    # The SPA feeds either band.
    for cx in (LX + HW / 2, RXX + HW / 2):
        s.arrow([(cx, spa["height"] + 13), (cx, BAND_TOP - 1.5)],
                colour=role("muted").stroke)

    # ---- the stores, one row per side --------------------------------------
    rows = [
        (0, "CALIBER metadata DB",
         "CALIBER_DATABASE_URL owns its own tables and Alembic history",
         "MLflow backend store",
         "a separate logical database, so the two Alembic histories never compete"),
        (0, "CALIBER storage service",
         "local or s3 ONLY; MinIO is reached as S3-compatible",
         "MLflow artifact root",
         "the full MLflow backend set, INCLUDING GCS"),
    ]
    row_y = band_bottom + 13
    for _, lt, ld, rt, rd in rows:
        lb = s.labelled_box(LX, row_y, HW, lt, ld, role_name="store",
                            title_size=SIZE_BODY, detail_size=SIZE_BODY)
        rb = s.labelled_box(RXX, row_y, HW, rt, rd, role_name="store",
                            title_size=SIZE_BODY, detail_size=SIZE_BODY)
        row_y = max(lb["y"] + lb["height"], rb["y"] + rb["height"]) + 6
    for cx in (LX + HW / 2, RXX + HW / 2):
        s.arrow([(cx, band_bottom + 1.5), (cx, band_bottom + 11)],
                colour=role("store").stroke)

    # ---- the trap this figure exists to defuse ------------------------------
    trap = s.labelled_box(
        LX, row_y + 5, W,
        'Two different stores are both called "the artifact store."',
        "Support on one is not support on the other: reading MLflow's GCS support "
        "as CALIBER storage support is a configuration error waiting to happen.",
        role_name="govern", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )
    s.text(W / 2, trap["y"] + trap["height"] + 6,
           "The API and the SPA are identical in both modes, so this is an "
           "operations decision, not a feature one.",
           size=SIZE_SMALL, italic=True, colour=INK)

    s.fit_height()
    return s
