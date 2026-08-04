"""Figure: data flow and state ownership.

Three stores, three owners, and the two crossings that no transaction can span.
Solid edges are transactional; the two dashed edges are the crossings that can
diverge and must be reconciled. Naming them is what lets the audit guarantee in the
paper be stated precisely instead of gestured at.

Coordinates are points at final printed size.
"""

from __future__ import annotations

from palette import INK, role
from scene import SIZE_BODY, SIZE_HEAD, Scene
from stats import stat

TARGET_WIDTH_PT = 494.0

WCOL = 80.0    # writers column
OX = 86.0      # relational owner
OW = 116.0
SX = 208.0     # object storage and MLflow
SW = 130.0
DX = 344.0     # relational domains
DW = 150.0
TOP = 13.0


def build() -> Scene:
    s = Scene("fig-dataflow", width=TARGET_WIDTH_PT, height=0)

    def head(x: float, label: str) -> None:
        s.text(x, 1, label, size=SIZE_BODY, bold=True, align="left",
               colour=role("muted").stroke)

    # ======================= writers ==========================================
    head(0, "WRITERS")
    y = TOP
    for t, d, r in [
        ("Route handler", "inline validation, then durable mutation", "surface"),
        ("Background loop", "a claimed job, writing the same tables", "async"),
        ("Apply + promoter", "the release path", "control"),
    ]:
        b = s.labelled_box(0, y, WCOL, t, d, role_name=r,
                           title_size=SIZE_BODY, detail_size=SIZE_BODY)
        s.arrow([(WCOL + 1, b["y"] + b["height"] / 2), (OX - 1, TOP + 34)])
        y = b["y"] + b["height"] + 6

    # ======================= the authoritative owners =========================
    head(OX, "AUTHORITATIVE OWNERS")
    rel = s.labelled_box(
        OX, TOP, OW, "Relational metadata",
        "OWNS the control plane:\n"
        "registries, jobs, approvals,\n"
        "runs, deployments, verdicts,\n"
        "audit log, effect ledger,\n"
        "and the file inventory\n"
        "NOT file bytes.\n"
        "NOT prompt versions.",
        role_name="store", title_size=SIZE_HEAD, detail_size=SIZE_BODY,
    )
    obj = s.labelled_box(
        SX, TOP, SW, "Object storage",
        "OWNS file bytes: uploads,\nartifacts, exports, archives\nNOT the inventory.",
        role_name="store", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )
    mlf = s.labelled_box(
        SX, obj["y"] + obj["height"] + 14, SW, "MLflow",
        "OWNS prompt registry versions,\naliases, traces, assessments\n"
        "NOT workflow or tool metadata.",
        role_name="extern", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )

    # The two crossings no single transaction can cover.
    for target in (obj, mlf):
        s.arrow(
            [(OX + OW + 1, rel["y"] + rel["height"] / 2),
             (target["x"] - 1, target["y"] + target["height"] / 2)],
            colour=role("govern").stroke, dashed=True, bold=True,
        )

    # ======================= relational domains ===============================
    head(DX, "RELATIONAL DOMAINS")
    dy = TOP
    for t, d in [
        ("Core governance", "verification items, refinement jobs, approvals"),
        ("Registries", "prompt, tool, skill, MCP, workflow versions"),
        ("Knowledge & quality", "bases, chunks, eval runs, judges, verdicts"),
        ("Platform", "projects, files, secrets, sessions, incidents"),
    ]:
        b = s.labelled_box(DX, dy, DW, t, d, role_name="muted",
                           title_size=SIZE_BODY, detail_size=SIZE_BODY)
        dy = b["y"] + b["height"] + 5
    # A partition of the relational store, not a fourth store.
    s.line([(DX - 4, TOP), (DX - 4, dy - 5)], dashed=True)

    # ======================= the two annotations ==============================
    note_y = max(rel["y"] + rel["height"], mlf["y"] + mlf["height"], dy) + 12
    note = s.labelled_box(
        OX, note_y, SX + SW - OX,
        "The two dashed crossings are dual-write boundaries",
        "A database-resident mutation and its audit_record call share ONE SQL\n"
        "transaction, so control-plane state and its audit row cannot diverge.\n"
        "Object-storage writes and external registry effects cannot join that\n"
        "transaction: they settle by idempotent retry, not by atomicity. Importing\n"
        "the audit helper does not prove every mutation in a module is audited.",
        role_name="govern", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )
    s.labelled_box(
        DX, note_y, DW, "Reader path",
        "A read resolves relational metadata first, then dereferences bytes or "
        "registry versions. The inventory is never rebuilt from a bucket listing.",
        role_name="store", title_size=SIZE_BODY, detail_size=SIZE_BODY,
    )
    s.text(
        TARGET_WIDTH_PT / 2, note["y"] + note["height"] + 6,
        f"{stat('statModels')} tables carry the control plane. Solid edges are "
        "transactional; the two dashed edges are the honest limit.",
        size=SIZE_BODY, italic=True, colour=INK,
    )

    s.fit_height()
    return s
