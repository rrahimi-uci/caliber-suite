#!/usr/bin/env python3
"""Generate the CALIBER seminar deck.

Every claim on a slide traces to the manuscript in ``paper/sections`` and every
number to ``paper/tex/macros.tex`` or ``paper/generated/stats.tex``. The two
places that discipline shows:

* :func:`stats` parses the generated counts rather than restating them, so a
  re-run of ``scripts/gen_stats.py`` moves the deck too. A number that drifts
  from the manuscript is a build failure, not a slide someone has to notice.
* The evaluation slide reports the table as empty. The manuscript's primary
  limitation is that its quantitative evaluation is specified and unrun, and a
  deck that quietly dropped that row would misrepresent the paper.

Usage::

    python3 -m venv .venv && .venv/bin/pip install python-pptx
    .venv/bin/python paper/slides/generate_slides.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deck_kit import (
    AMBER,
    AMBER_INK,
    CR,
    CW,
    EYEBROW_Y,
    FLOOR,
    INK,
    ML,
    MUTED,
    MUTED_DARK,
    NAVY,
    PANEL,
    RULE_DARK,
    TEAL,
    TEAL_BRIGHT,
    TEAL_PALE,
    TITLE_Y,
    WARM,
    WARM_INK,
    WHITE,
    Deck,
    check_floor,
    measure,
)

HERE = Path(__file__).resolve().parent
PAPER = HERE.parent
OUT = HERE / "caliber-layered-control-plane.pptx"

FOOTER = "CALIBER  ·  A Layered Control Plane for AI Agent Governance, Workflow Orchestration, and Progressive Autonomy"


# --------------------------------------------------------------------------- #
# Numbers, read from the manuscript rather than retyped
# --------------------------------------------------------------------------- #

def stats() -> dict[str, str]:
    """Every count the deck states, sourced from the paper's own macro files."""
    text = (PAPER / "tex" / "macros.tex").read_text(encoding="utf-8")
    gen = PAPER / "generated" / "stats.tex"
    if gen.exists():
        text += gen.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for name, value in re.findall(
        r"\\(?:new|renew|provide)command\{\\(stat[A-Za-z]+)\}\{([^}]*)\}", text
    ):
        found[name] = value          # later definitions win, as in LaTeX
    required = [
        "statLayers", "statModes", "statFamilies", "statScopes", "statLoops",
        "statChainTerms", "statLayersW", "statModesW", "statFamiliesW",
        "statScopesW", "statLoopsW", "statChainTermsW", "statLayersC",
        "statModesC", "statFamiliesC", "statLoopsC", "statChainTermsC",
        "statPyLoc", "statUiLoc", "statRouteDecls",
        "statRouteModules", "statMigrations", "statModels", "statSchemas",
        "statTestLoc", "statNodeTypes",
    ]
    missing = [k for k in required if k not in found]
    if missing:
        raise SystemExit(f"missing counts in the paper's macros: {missing}")
    return found


S = stats()


# --------------------------------------------------------------------------- #
# Layout helpers shared by several slides
# --------------------------------------------------------------------------- #

def columns(n: int, gap: float = 12.0, width: float = CW,
            left: float = ML) -> tuple[list[float], float]:
    """``n`` equal columns across ``width``. Returns the x positions and one width."""
    w = (width - gap * (n - 1)) / n
    return [left + i * (w + gap) for i in range(n)], w


def card_grid(s, items, x0, y, w, tone="plain", *, cols=None, gap=12.0,
              rgap=12.0, title_size=12.0, body_size=10.0, glyph=None,
              glyph_fill=None, min_h=0.0, bottom=None):
    """A row (or grid) of titled cards. ``items`` are ``(title, body)`` pairs.

    Card height is *measured*, never assumed: a row is as tall as its tallest
    card needs to be. Hand-set heights are what produced clipped copy in the
    first draft of this deck, and the fit checker caught every one of them.
    Returns the ``y`` below the grid.
    """
    cols = cols or len(items)
    xs, cw = columns(cols, gap, w, x0)
    from deck_kit import TONES

    _, _, accent, body_color = TONES[tone]
    pad_t, pad_b, gutter = 15.0, 15.0, 7.0
    head_w = cw - 67 if glyph else cw - 38
    body_w = cw - 38

    geom = []
    for head, body in items:
        hh = measure(head, head_w, title_size, bold=True, spacing=1.12)
        if glyph:
            hh = max(hh, 26.0)
        bh = measure(body, body_w, body_size, spacing=1.28)
        geom.append((hh, bh, pad_t + hh + gutter + bh + pad_b))

    rows = [geom[i:i + cols] for i in range(0, len(geom), cols)]
    heights = [max(max(g[2] for g in row), min_h) for row in rows]
    if bottom is not None:
        # Cards in a row share a head baseline, so the slack is absorbed as extra
        # card height with the copy still top-aligned -- and it is capped, because
        # a card twice its content's height reads as unfinished rather than airy.
        slack = bottom - (y + sum(heights) + rgap * (len(heights) - 1))
        if slack > 0:
            grow = min(slack / len(heights), 30.0)
            heights = [h + grow for h in heights]

    for i, (head, body) in enumerate(items):
        r, c = divmod(i, cols)
        cx = xs[c]
        cy = y + sum(heights[:r]) + r * rgap
        hh, bh, _ = geom[i]
        s.card(cx, cy, cw, heights[r], tone)
        if glyph:
            s.badge(cx + 17, cy + pad_t, 24,
                    glyph[i] if isinstance(glyph, list) else glyph,
                    glyph_fill or accent, size=10.5)
            s.text(cx + 48, cy + pad_t, head_w, hh, head, size=title_size,
                   bold=True, color=accent, spacing=1.12, check="cardhead")
        else:
            s.text(cx + 19, cy + pad_t, head_w, hh, head, size=title_size,
                   bold=True, color=accent, spacing=1.12, check="cardhead")
        s.text(cx + 19, cy + pad_t + hh + gutter, body_w, bh, body,
               size=body_size, color=body_color, spacing=1.28,
               check="cardbody")

    bottom = y + sum(heights) + rgap * (len(heights) - 1)
    check_floor(s.number, bottom, "cards")
    return bottom


def note(s, y, kicker, body, tone="neutral", *, h=None, x=ML, w=CW,
         bottom=None):
    from deck_kit import TONES

    _, _, accent, body_color = TONES[tone]
    inner = w - 40
    bh = measure(body, inner, 10.0, spacing=1.28)
    h = h or bh + 46
    if bottom is not None:
        h = max(h, bottom - y)
    s.card(x, y, w, h, tone)
    ty = y + 11 + (h - (bh + 46)) / 2
    s.text(x + 20, ty, inner, 14, kicker, size=9.5, bold=True,
           color=accent, caps=True, check="notek")
    s.text(x + 20, ty + 18, inner, bh, body, size=10.0, color=body_color,
           spacing=1.28, check="noteb")
    check_floor(s.number, y + h, "note")
    return y + h


def divider(deck, numeral, title, blurb, notes=""):
    s = deck.slide(NAVY, notes=notes)
    s.ellipse(734, 72, 403, PANEL)
    s.ellipse(835, 172, 201, TEAL)
    s.ellipse(896, 233, 79, TEAL_BRIGHT)
    s.text(ML, 169, 130, 108, numeral, size=76, bold=True, color=TEAL_BRIGHT,
           spacing=1.0)
    s.text(186, 180, 500, 54, title, size=34, bold=True, color=WHITE,
           spacing=1.05, check="divtitle")
    s.text(188, 240, 500, 60, blurb, size=13.5, color=TEAL_PALE, spacing=1.32,
           check="divblurb")
    return s


# --------------------------------------------------------------------------- #
# Slides
# --------------------------------------------------------------------------- #

def title_slide(deck: Deck) -> None:
    s = deck.slide(NAVY, chrome=False, notes=(
        "CALIBER is a layered control plane for the lifecycle of AI-agent "
        "resources. The talk has three movements: the abstraction and its "
        "architecture; the per-family governance claim, which is the paper's "
        "actual intellectual content; and the evidence, comparison, and limits. "
        "State up front that the quantitative evaluation is specified and unrun."
    ))
    s.ellipse(712, -123, 489, PANEL)
    s.ellipse(820, -8, 259, TEAL)
    s.ellipse(892, 64, 115, TEAL_BRIGHT)

    s.text(ML, 104, 640, 21,
           "AI-AGENT RESOURCES  ·  RELEASE ENGINEERING  ·  CONTROL-PLANE ARCHITECTURE",
           size=10.5, bold=True, color=TEAL_BRIGHT, caps=True, check="kicker")
    s.text(ML, 130, 700, 92, "CALIBER", size=66, bold=True, color=WHITE,
           spacing=1.0)
    s.text(ML, 228, 660, 62,
           "A Layered Control Plane for AI Agent Governance,\nWorkflow Orchestration, and Progressive Autonomy",
           size=22, bold=True, color=WHITE, spacing=1.22, check="subtitle")
    s.text(ML, 300, 620, 48,
           "The governed asset is a typed record with version history, an authority "
           "model, an audit trail, and — where its family supports one — a live "
           "target that client code resolves at call time.",
           size=12.5, color=TEAL_PALE, spacing=1.32, check="blurb")

    s.rule(ML, 372, 620, RULE_DARK)
    cols = [
        ("Scope", f"{S['statFamilies']} asset families, {S['statLayers']} layers,\n"
                  f"{S['statModes']} lifecycle modes"),
        ("Implementation", f"{S['statPyLoc']} lines of Python,\n"
                           f"{S['statUiLoc']} of TypeScript"),
        ("Standing", "An architectural claim;\nthe evaluation is unrun"),
    ]
    xs, w = columns(3, 18, 620, ML)
    for i, (head, body) in enumerate(cols):
        s.text(xs[i], 386, w, 21, head, size=13, bold=True, color=TEAL_BRIGHT,
               check="colhead")
        s.text(xs[i], 409, w, 50, body, size=10.5, color=MUTED_DARK,
               spacing=1.32, check="colbody")
    s.text(ML, 468, 700, 21,
           "Technical seminar  ·  Reza Rahimi  ·  JazzX AI  ·  August 2026",
           size=10.5, color="43597C")


def s_motivation_steps(deck: Deck) -> None:
    s = deck.slide(notes=(
        "The scenario is a support agent citing a stale refund policy. Everything "
        "up to the flag is well tooled. Walk the nine steps and name the tooling "
        "for each; the point is that steps 6-9 are answered today for prompts by "
        "prompt registries and remain open for every other resource family."
    ))
    y = s.head(
        "Motivation  ·  §2.1",
        "Detection is well tooled. Remediation is nine manual steps.",
        "A reviewer flags a wrong answer. The trace carries the retrieved chunks and "
        "the rendered prompt, and the judgement is recorded against it. Then enumerate "
        "what a team must actually do to fix it, and what tooling exists for each step.",
    )

    steps = [
        ("1", "Decide it is real", "a queue, if\nsomeone built one", "amber"),
        ("2", "Locate the cause", "reading the\ntrace", "amber"),
        ("3", "Assemble evidence", "none standard;\nhand-built sheets", "warm"),
        ("4", "Produce a candidate", "an optimizer,\nrun out of band", "amber"),
        ("5", "Measure it", "a harness,\nrun manually", "amber"),
        ("6", "Decide to ship", "a human,\ninformally", "warm"),
        ("7", "Release it", "a code\ndeployment", "warm"),
        ("8", "Record the change", "a commit message,\nif anyone writes one", "warm"),
        ("9", "Be able to undo it", "git revert plus\nanother deployment", "warm"),
    ]
    y = s.label(ML, y, CW, "Nine steps, and the tooling each one actually has")
    xs, w = columns(9, 7.0)
    for i, (num, head, tool, tone) in enumerate(steps):
        from deck_kit import TONES

        _, _, accent, body_color = TONES[tone]
        s.card(xs[i], y, w, 128, tone)
        s.badge(xs[i] + 9, y + 10, 21, num, accent, size=9.5)
        s.text(xs[i] + 9, y + 39, w - 18, 40, head, size=10.0, bold=True,
               color=accent, spacing=1.16, check="stephead")
        s.text(xs[i] + 9, y + 84, w - 18, 34, tool, size=8.5,
               color=body_color, spacing=1.20, check="steptool")
    y += 140

    y = card_grid(
        s,
        [("What prompt registries already close",
          "MLflow, LangSmith and Langfuse give immutable versions, a named pointer "
          "the client resolves at call time, rollback by re-pointing, and permissions. "
          "For prompts, steps 6–9 are a solved problem and CALIBER re-implements it."),
         ("What is still open for everything else",
          "Workflows, tool definitions, MCP server configurations, skills and retrieval "
          "corpora are not registry citizens with versions and pointers — and no "
          "documented mechanism makes a promotion fail because a score fell.")],
        ML, y, CW, cols=2, gap=16, bottom=442,
    ) + 14
    s.text(ML, y, CW, 18,
           "The gap is not model quality or observability. It is cross-resource release "
           "engineering for non-code artifacts.",
           size=10.5, bold=True, color=MUTED, check="close")


def s_obvious_answers(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Four answers a reviewer will reach for, and why each is insufficient. The "
        "fourth is the contestable one: we argue against auto-apply, and D4 records "
        "the price — refinement throughput is bounded by operator attention."
    ))
    y = s.head(
        "Motivation  ·  §2.2–2.3",
        "Why the obvious answers do not close it",
        "Each of these is genuinely useful and CALIBER uses three of them. None of "
        "them is a release path for a non-code artifact.",
    )
    y = card_grid(
        s,
        [("“Put the prompts in git.”",
          "History and review, yes — and necessary. But no live target a running "
          "agent resolves without redeployment, no evidence attached to a version, "
          "and no record that an operator acting under a declared scope authorized "
          "the change. It binds the prompt's release to the application's."),
         ("“Use the prompt platform's evaluation.”",
          "These platforms do own versioned prompt artifacts and link evaluations "
          "to them; pretending otherwise erases the closest baseline. The narrower "
          "limitation is scope: workflows, tools, MCP configurations, skills and "
          "corpora get no compatible release evidence or rollback semantics."),
         ("“Use an optimizer.”",
          "DSPy, GEPA, MIPRO, OPRO and TextGrad are real advances. But an optimizer "
          "produces a candidate, not a release: no notion of who may promote, what "
          "the outgoing live target was, or how to restore it. It is the compiler, "
          "not the deployment system."),
         ("“Automate the whole loop.”",
          "The tempting answer, and we argue it is wrong at the current state of "
          "the art. A corpus is a sample; judges correlate with each other and with "
          "the model under test. Auto-apply converts a visible, reviewable change "
          "into an invisible, correlated one.")],
        ML, y, CW, tone="warm", cols=4, gap=12,
        glyph=["✕", "✕", "✕", "✕"], bottom=352,
    ) + 14

    y = s.label(ML, y, CW, "Five requirements the scenario yields",
                color=TEAL)
    reqs = [
        ("R1", "Typed, versioned artifacts", "a record with a schema and an\nimmutable history, not a string"),
        ("R2", "Late-bound live targets",
         "the client resolves which version to run at call time"),
        ("R3", "Evidence bound to versions", "scores and their corpus attached\nto the version they describe"),
        ("R4", "An enforced gate, plus a human", "one check unbypassable, one\njudgement a person's"),
        ("R5", "Audited, reversible release",
         "the release records what the live target was, so undo is a restore"),
    ]
    xs, w = columns(5, 11)
    for i, (tag, head, body) in enumerate(reqs):
        s.card(xs[i], y, w, 92, "teal")
        s.text(xs[i] + 14, y + 11, 30, 15, tag, size=10.0, bold=True,
               color=TEAL)
        s.text(xs[i] + 14, y + 27, w - 28, 24, head, size=10.5, bold=True,
               color=NAVY, spacing=1.10, check="reqhead")
        s.text(xs[i] + 14, y + 54, w - 28, 32, body, size=8.5, color=INK,
               spacing=1.22, check="reqbody")
    check_floor(s.number, y + 92, "requirements")


def s_positioning(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Read this table as a positioning statement, not a ranking — these systems "
        "are not substitutes. The top block is the closest baseline and the honest "
        "reading is that CALIBER re-implements a solved problem for prompts. The "
        "delta is the last two columns."
    ))
    y = s.head(
        "Related work  ·  §11  ·  Tables 10 and 11",
        "Positioning: what the adjacent systems already own",
        "Late-bound pointer indirection for prompts is not novel; neither is rollback "
        "by re-pointing, nor restricting who may promote. Those are table stakes.",
    )
    rows = [
        ["MLflow Prompt Registry", "the trace, plus the versioned prompt",
         "yes — registry aliases", "linked, not enforced", "prompts, models"],
        ["LangSmith", "the trace, plus the versioned prompt",
         "yes — labels and tags", "linked, not enforced", "prompts"],
        ["Langfuse", "the trace, plus the versioned prompt",
         "yes — labels", "linked, not enforced", "prompts"],
        ["Phoenix", "the trace", "tracing only", "no", "—"],
        ["LangChain / LangGraph", "the chain or graph", "no", "no",
         "— composition"],
        ["AutoGen / CrewAI", "the agent and its conversation", "no", "no",
         "— orchestration"],
        ["DSPy / GEPA / MIPRO", "the program being optimized", "no",
         "produces candidates, not releases", "— optimization"],
        ["promptfoo / Evals", "the test case", "no", "reports, does not gate",
         "— evaluation"],
        ["CALIBER", "the governed asset, across families",
         "yes, where the family supports one",
         "enforced on candidate advancement",
         f"{S['statFamilies']} families"],
    ]
    y = s.table(ML, y, [148, 196, 168, 178, 169],
                ["System", "Organizing abstraction", "Versioned + live pointer",
                 "Evidence gates release", "Families governed"],
                rows, highlight=8, bottom=392)
    note(s, y + 14, 
         "The honest reading",
         "All three prompt platforms link evaluations to prompts; none of their "
         "documentation describes a mechanism that makes a promotion fail because a "
         "score fell. In CALIBER the advancement gate is such a mechanism — inside "
         "the refinement state machine, and, as §12 concedes, only there.",
         "neutral")


def s_claim(deck: Deck) -> None:
    s = deck.slide(notes=(
        "This is the slide to spend time on. The claim is deliberately narrow: it is "
        "not that layering makes governance uniform. It is that adjacency confers "
        "availability and only wiring confers enforcement — and that this follows "
        "from the heterogeneity of the artifact types, not from our engineering."
    ))
    y = s.head(
        "The central claim  ·  §1 and §5.1",
        "The claim, stated narrowly enough to be wrong",
    )
    s.card(ML, y, CW, 96, "teal")
    s.rect(ML, y, 3.5, 96, TEAL)
    s.text(ML + 26, y + 16, CW - 52, 66,
           "Adjacency in a layered architecture confers capability availability, not "
           "capability inheritance. A family placed in the asset layer obtains lifecycle "
           "behaviour and governance enforcement only by explicitly wiring them. The "
           "shared base contract is identity, history, authority, and audit; release, "
           "rollback, evaluation, and evidence are capability-specific obligations.",
           size=13.5, italic=True, color=NAVY, spacing=1.34, check="quote")
    y += 108

    y = s.label(ML, y, CW, "Why a single uniform contract cannot be written")
    y = card_grid(
        s,
        [("Prompt", "A live target is an MLflow registry alias. Release rotates it; "
                    "rollback restores the target it previously held. This family has "
                    "the full release vocabulary."),
         ("Test set", "There is no live target at all. A test set *is* evidence. "
                      "Forcing it to have a release path would produce a field that "
                      "nothing reads."),
         ("Judge", "The artifact is a scorer, referenced by token wherever a scorer "
                   "is accepted. No live target, no evidence base of its own — what "
                   "it has is a measured agreement rate with human labels.")],
        ML, y, CW, cols=3, gap=14,
    ) + 12
    note(s, y,
         "What follows, and what does not",
         "Any single contract all three satisfy is vacuous — it can require nothing "
         "stronger than “has an identifier.” A contract strong enough to be worth "
         "enforcing on prompts would exclude test sets and judges, making the platform "
         "less useful rather than more principled. Uniform guarantees are impossible "
         "across genuinely heterogeneous families; uniform obligations are possible, "
         "and require a mechanism CALIBER does not yet have.",
         "neutral")


def s_asset(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Twelve facets, four universal. The four are what make a record governed at "
        "all; the eight are the subject of the per-family argument. Close on what a "
        "governed asset is not — the three negatives are the ones adopters conflate."
    ))
    y = s.head(
        "The abstraction  ·  §4.1",
        "The governed asset: twelve facets, four of them universal",
        "The unit of value is a typed record carrying enough metadata that a change "
        "to it is reviewable after the fact. Four facets hold for every family.",
    )
    y = card_grid(
        s,
        [("Typed definition", "A schema-validated specification that is the source "
                              "of truth for the asset, not a rendering of it."),
         ("Version history", "Immutable snapshots, or immutable registry versions "
                             "held externally."),
         ("Authority model", f"Which of the {S['statScopesW']} RBAC scopes may read, "
                             "mutate, and release it."),
         ("Trace and audit trail", "The durable record of what was done to it, and "
                                   "by whom.")],
        ML, y, CW, tone="teal", cols=4, gap=13,
    ) + 12

    y = s.label(ML, y, CW,
                "The other eight facets are family-specific — and that asymmetry is §5",
                color=MUTED)
    facets = ["Live target", "Test surface", "Evidence base", "Evaluation",
              "Gate semantics", "Calibration", "Release path", "Packaging"]
    xs, w = columns(8, 9)
    for i, f in enumerate(facets):
        s.card(xs[i], y, w, 30, "plain")
        s.text(xs[i] + 6, y + 8, w - 12, 16, f, size=9.5, bold=True,
               color=INK, align="c", check="facet")
    y += 44

    y = card_grid(
        s,
        [("Not a prompt file in git",
          "Version control gives history but no live target, no bound evidence, and "
          "no authority model."),
         ("Not a dashboard over traces",
          "A dashboard describes behaviour but owns no artifact, and cannot make a "
          "change to one reviewable."),
         ("Not an agent",
          "Agents are composed in a framework of the team's choosing and are clients "
          "of governed assets.")],
        ML, y, CW, tone="warm", cols=3, gap=14, glyph="✕", bottom=FLOOR,
    )


def s_modes(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Six verbs acting on the nouns. Stress that they are reusable concepts, not "
        "a state machine every asset traverses — a test set is authored, tested "
        "against, and observed, but is never released. Separating modes from families "
        "is what makes the coverage matrix expressible at all."
    ))
    y = s.head(
        "Lifecycle modes  ·  §4.2  ·  L5",
        f"{S['statModesC']} lifecycle modes, deliberately few",
        "The modes are the verbs that act on the governed nouns. They are reusable "
        "concepts, not a state machine every asset traverses.",
    )
    y = card_grid(
        s,
        [("Author", "Draft, render, compile, validate. Authoring is deliberately "
                    "non-live: registering a version cannot move a pointer."),
         ("Test", "Bounded runs against fixtures, before anything is scored or "
                  "proposed for advancement."),
         ("Evaluate", "Scorecards, deterministic scorers, authored LLM judges, and "
                      "review queues."),
         ("Calibrate", "Propose a measurably better version — the optimizer's output "
                       "is a candidate, never a release."),
         ("Release", "Gate, promote, roll back. The outgoing live target is recorded, "
                     "so undo is a restore."),
         ("Observe", "Trace, meter, incident, replay — and the source of the next "
                     "signal.")],
        ML, y, CW, tone="teal", cols=3, gap=14, rgap=14,
        glyph=["1", "2", "3", "4", "5", "6"],
    ) + 12
    note(s, y,
         "Why the modes are named separately from the families",
         "A test set is authored, tested against, and observed, but is never released "
         "— it is the evidence others are released against. A judge is authored and "
         "evaluated for human agreement, but has no live target. Enumerating the modes "
         "separately from the families is what makes a coverage matrix expressible; "
         "without that separation there is only a single undifferentiated notion of "
         "“supported.”",
         "neutral")


def s_chain(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Seven concepts, not seven stages. The concrete prompt path has six numbered "
        "stages and the correspondence is not bijective: Evidence has no stage of its "
        "own, Signal spans both a trace and a human confirmation, and Trace is both "
        "the seventh term and the input to the next iteration. The lower register — "
        "the durable residue — is what distinguishes this from an observability pipeline."
    ))
    y = s.head(
        "The governance chain  ·  §4.3  ·  Figure 3",
        f"{S['statChainTermsC']} terms, and the durable residue each one leaves",
        "The chain is what makes a change reviewable. Each term deposits state, so "
        "six weeks later “why is the prompt like this, and what did we know when we "
        "changed it” is answerable from records rather than from recollection.",
    )
    terms = [
        ("Signal", "a failure worth acting on",
         "a verification item"),
        ("Evidence", "traces and examples assembled into a corpus",
         "a refinement job with its assembled evidence"),
        ("Candidate", "a proposed better version",
         "a diagnosis and a candidate artifact"),
        ("Measurement", "scored against that corpus",
         "scores with an enforced gate decision"),
        ("Decision", "an operator applies, or does not",
         "an explicit apply action with a provenance anchor"),
        ("Release", "the live target moves",
         "a rollback checkpoint and an audit row"),
        ("Trace", "the next signal arrives",
         "new traces, and the next iteration"),
    ]
    xs, w = columns(7, 9)
    for i, (name, gloss, residue) in enumerate(terms):
        s.card(xs[i], y, w, 106, "teal")
        s.text(xs[i] + 10, y + 13, w - 20, 16, name, size=11.0, bold=True,
               color=TEAL, check="term")
        s.text(xs[i] + 10, y + 35, w - 20, 58, gloss, size=8.5, color=INK,
               spacing=1.22, check="gloss")
        if i < 6:
            s.text(xs[i] + w - 1, y + 44, 14, 18, "›", size=13, bold=True,
                   color=TEAL_BRIGHT, align="c")
    y += 114

    s.label(ML, y, CW, "The durable residue — the lower register of Figure 3",
            color=MUTED)
    y += 20
    for i, (_, _, residue) in enumerate(terms):
        s.card(xs[i], y, w, 76, "plain")
        s.text(xs[i] + 10, y + 12, w - 20, 54, residue, size=8.5, color=MUTED,
               spacing=1.22, check="residue")
    y += 88

    note(s, y,
         "A misreading the figure is drawn to prevent",
         "These are seven concepts, not seven stages. The concrete prompt path has six "
         "numbered stages and the correspondence is not bijective: Evidence has no "
         "stage of its own, Signal spans both an incoming production trace and the "
         "human confirmation that the failure is real, and Trace is simultaneously the "
         "seventh term and the input to the next iteration.",
         "amber", bottom=FLOOR)


def s_layers(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Read bottom-up. L1 is swappable by configuration; L3 is a substrate of "
        "shared primitives that asset paths wire explicitly — that word 'explicitly' "
        "is the whole argument. Two runtime properties are load-bearing: CALIBER is a "
        "sibling surface to MLflow rather than a proxy, and there is no worker tier."
    ))
    y = s.head(
        "Architecture  ·  §5  ·  Figures 1 and 2",
        f"{S['statLayersC']} layers, read bottom-up",
        "One ASGI application, a React SPA served from the same origin, and the "
        "state it owns. The dashed left rail cuts across every layer.",
        lede_h=22,
    )
    bands = [
        ("L6", "Surfaces", f"the React SPA, a served management API of {S['statRouteDecls']} "
                           f"route declarations across {S['statRouteModules']} modules, a "
                           "thin Python SDK and CLI over that contract, and the Aria "
                           "copilot's permissioned agentic tool loop", "teal"),
        ("L5", "Lifecycle modes", "Author, Test, Evaluate, Calibrate, Release, Observe "
                                  "— the verbs", "teal"),
        ("L4", "Asset families", f"{S['statFamiliesW']} governed families — the nouns. Six "
                                 "authored runtime assets, two evidence and scoring "
                                 "assets, one anchor record", "teal"),
        ("L3", "Governance", "shared primitives — evidence, evaluation, gates, audit, "
                             "authority — available to every family and applied only by "
                             "those that wire them", "amber"),
        ("L2", "Kernel", f"the modular services built at application startup; "
                         f"{S['statModels']} ORM models and {S['statSchemas']} schemas. "
                         "The only tier that touches state or external systems", "plain"),
        ("L1", "Infrastructure", f"PostgreSQL, object storage, MLflow, providers — all of "
                                 f"it swappable by configuration; {S['statMigrations']} "
                                 "migrations", "plain"),
    ]
    from deck_kit import TONES

    rail_w = 116.0
    band_x = ML + rail_w + 14
    band_w = CR - band_x
    bh = 38.0
    for i, (tag, name, body, tone) in enumerate(bands):
        by = y + i * (bh + 6)
        _, _, accent, body_color = TONES[tone]
        s.card(band_x, by, band_w, bh, tone)
        s.text(band_x + 14, by + 12, 26, 16, tag, size=10.5, bold=True,
               color=accent)
        s.text(band_x + 46, by + 11, 130, 18, name, size=11.5, bold=True,
               color=accent, check="bandname")
        s.text(band_x + 186, by + 8, band_w - 202, bh - 14, body, size=9.0,
               color=body_color, spacing=1.20, check="bandbody")

    rail_h = 6 * bh + 5 * 6
    s.card(ML, y, rail_w, rail_h, "neutral")
    s.text(ML + 12, y + 13, rail_w - 24, 14, "Platform services",
           size=9.5, bold=True, color=NAVY, check="railhead")
    s.text(ML + 12, y + 32, rail_w - 24, rail_h - 44,
           "evidence base\nevaluation\ncalibration\ncapability registry\n"
           "integration hub\nproject scoping",
           size=9.0, color=INK, spacing=1.42, check="railbody")
    y += rail_h + 14

    y = card_grid(
        s,
        [("A sibling surface, not a proxy",
          "CALIBER and MLflow each own their own store. That is why the reconciliation "
          "boundary exists and cannot be designed away — D1 records it as the price."),
         ("No separate worker tier",
          f"Queued work is drained by up to {S['statLoopsW']} in-process loops arbitrated "
          "by claim and lease columns. One deployable, no split-brain — and loop "
          "capacity coupled to request capacity.")],
        ML, y, CW, cols=2, gap=16,
    )


def s_families(deck: Deck) -> None:
    s = deck.slide(notes=(
        "The paper's most important table and the hardest to write honestly. Its "
        "whole function is to refute the reading that a shared substrate implies "
        "shared guarantees. Point at the shared version panel: it is mounted for five "
        "families through per-artifact adapters — sharing the component does not "
        "share the semantics, and that is the canonical adopter trap."
    ))
    y = s.head(
        "Per-family governance  ·  §5.4  ·  Table 3",
        f"{S['statFamiliesC']} families, {S['statFamiliesW']} guarantee surfaces",
        "Six are authored runtime assets, two are evidence and scoring assets, and "
        "one is the anchor record that items, jobs and approvals hang off.",
        lede_h=22,
    )
    def chip(word, color, phrase="", nl=False):
        """A chip plus the phrase it qualifies. Two chips in one cell go on two
        lines, as the chip/newline/chip pattern does in Table 3 -- running them
        together on one line reads as a single verdict."""
        tail = ("  " + phrase if phrase else "") + ("\n" if nl else "")
        return [(word, color, True)] + ([(tail, None, False)] if tail else [])

    No = chip("none", WARM)
    NAo = chip("n/a", MUTED)
    rows = [
        ["Prompt", "immutable MLflow registry versions behind an alias such as @prod",
         chip("enforced", TEAL, "advancement to candidate_ready", nl=True)
         + chip("advisory", AMBER, "per-version verdict"),
         "operator- or admin-scoped; records and restores the outgoing alias target",
         "provider optimizer + EvalProvider"],
        ["Workflow", "editable drafts promoted to published rows; deployment aliases select one",
         chip("enforced", TEAL, "readiness", nl=True)
         + chip("enforced", TEAL, "deploy gate, optimistic alias check"),
         "rollback pops the deployment's checkpoint stack", "manifest replay"],
        ["Knowledge base", "immutable build versions behind active_version_id",
         chip("none", WARM, "prompt-style verdict"),
         "audited activation; rollback derives the prior build from history",
         "retrieval-quality calibration"],
        ["Skill", "a mutable current record plus immutable snapshots",
         chip("enforced", TEAL, "advancement", nl=True)
         + chip("none", WARM, "release gate"),
         "rollback restores the prior snapshot as a new version",
         "agent-free optimizer path"],
        ["Tool", "separate (name, version) rows with a lifecycle status", No,
         "read-only history; no live alias", "revision-fenced deterministic suites"],
        ["Test set", "a version counter plus per-example validity intervals",
         chip("n/a", MUTED, "it is the evidence"),
         chip("none", WARM, "no live alias or rollback"), NAo],
        ["MCP server", "mutable managed definitions with audited edit history",
         "production workflow preflight",
         chip("none", WARM, "no version rollback; connection and policy "
                             "controls are fail-closed"),
         "connection and policy tests"],
        ["Judge", "operator-authored, reusable by a Judge.<id> token",
         chip("n/a", MUTED, "it is a scorer"), NAo,
         "human-alignment agreement (κ)"],
        ["Agent", "the anchor record that items, jobs and approvals attach to", NAo,
         "enabled is the pause/resume lever the workers read", NAo],
    ]
    y = s.table(ML, y, [96, 196, 172, 210, 185],
                ["Family", "History & liveness", "Gate semantics",
                 "Release / rollback", "Calibration idiom"],
                rows, size=8.0, pad=6.0, gutter=8.0, bottom=430)
    s.text(ML, y + 14, CW, 40, [
        [("enforced", TEAL, True), (" — unbypassable  ·  ", MUTED, False),
         ("advisory", AMBER, True),
         (" — filed as evidence, blocks nothing  ·  ", MUTED, False),
         ("none / n/a", WARM, True),
         (" — a facet the family does not implement: an architectural fact, "
          "not an omitted measurement", MUTED, False)],
        [("The shared version-history component is mounted for five families through "
          "per-artifact adapters: sharing the component does not share the semantics.",
          MUTED, True)],
    ], size=9.5, spacing=1.30, check="legend")
    check_floor(s.number, y + 52, "legend")


def s_third_design(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Do not present the choice as binary — an earlier draft did, and it was a "
        "false dichotomy. The third design is the ordinary answer to heterogeneous "
        "types in a typed system, it is strictly better in one specific respect, and "
        "we did not take it for a historical rather than a principled reason."
    ))
    y = s.head(
        "Per-family governance  ·  §5.1",
        "The third design, which we did not take",
        "Between one uniform contract and per-family wiring there is a well-known "
        "third option, and presenting the choice as binary would be a false dichotomy.",
    )
    y = card_grid(
        s,
        [("A thin base contract",
          "Every governed asset satisfies identity, immutable version history, an "
          "authority check, and an audit record. That much genuinely can be uniform, "
          "and CALIBER already holds it."),
         ("Orthogonal capability interfaces",
          "Releasable, Rollbackable, EvidenceBearing — implemented only by the "
          "families that possess them. Per-family variation remains, because a test "
          "set genuinely has no live target."),
         ("Obligations checked, not documented",
          "A family declaring Releasable could be required to supply a live-target "
          "resolver, a checkpoint writer, and a rollback path — checked by the type "
          "system or a start-up registry projection, not by a reviewer reading a table.")],
        ML, y, CW, tone="teal", cols=3, gap=14, bottom=272,
    ) + 14

    xs, w = columns(2, 16)
    s.card(xs[0], y, w, 140, "warm")
    s.badge(xs[0] + 18, y + 16, 24, "✕", WARM, size=10.5)
    s.text(xs[0] + 50, y + 16, w - 68, 22, "Why we did not take it",
           size=12.5, bold=True, color=WARM, check="whyhead")
    s.text(xs[0] + 19, y + 52, w - 38, 92,
           f"The reason is historical rather than principled. CALIBER's families were "
           f"wired one at a time as they were needed, and by the time the common shape "
           f"was visible the wiring already existed in {S['statFamiliesW']} places. "
           f"Retrofitting capability interfaces is the natural next step, and §13 "
           f"records it as such.",
           size=10.0, color=WARM_INK, spacing=1.28, check="whybody")

    s.card(xs[1], y, w, 140, "teal")
    s.badge(xs[1] + 18, y + 16, 24, "✓", TEAL, size=10.5)
    s.text(xs[1] + 50, y + 16, w - 68, 22, "What we therefore claim",
           size=12.5, bold=True, color=TEAL, check="claimhead")
    s.text(xs[1] + 19, y + 52, w - 38, 92,
           "Uniform guarantees are impossible across genuinely heterogeneous families, "
           "because the guarantees differ in kind. Uniform obligations are possible, "
           "but require a mechanism CALIBER does not have. Documentation-only "
           "enforcement is technical debt, not an impossibility result.",
           size=10.0, color=INK, spacing=1.28, check="claimbody")
    y += 152
    s.text(ML, y, CW, 18,
           "That is a narrower claim than “uniformity is impossible”, and it is the one "
           "the paper can defend.",
           size=10.5, bold=True, color=MUTED, check="close")


def s_gates(deck: Deck) -> None:
    s = deck.slide(notes=(
        "D3, and the decision most likely to be contested. The argument is about "
        "operator behaviour rather than about correctness: a blocking gate must be "
        "overridable, the override becomes the normal path, and the gate stops "
        "carrying information. Concede the scope — the enforced gate lives inside the "
        "refinement state machine and the direct promotion endpoint bypasses it."
    ))
    y = s.head(
        "Capabilities  ·  §7.3 and §6  ·  D3",
        "One enforced gate, one advisory verdict — and why they differ",
        "“Gated” names two mechanisms in CALIBER. Keeping them apart is what stops "
        "operators from routing around the system.",
    )
    xs, w = columns(2, 18)

    s.card(xs[0], y, w, 152, "teal")
    s.badge(xs[0] + 20, y + 18, 26, "✓", TEAL, size=11)
    s.text(xs[0] + 56, y + 17, w - 76, 24, "Candidate-advancement gate",
           size=14, bold=True, color=TEAL, check="gh1")
    s.text(xs[0] + 56, y + 41, w - 76, 16, "enforced · returns control flow",
           size=9.5, bold=True, color=TEAL, caps=True)
    s.text(xs[0] + 21, y + 66, w - 42, 74,
           "A candidate that fails it never reaches review. The review queue therefore "
           "contains only candidates that passed their own evaluation, and an operator's "
           "attention is spent on judgement rather than on triage — which matters, "
           "because operator attention is the binding constraint on this path.",
           size=10.0, color=INK, spacing=1.28, check="gb1")

    s.card(xs[1], y, w, 152, "amber")
    s.badge(xs[1] + 20, y + 18, 26, "!", AMBER, size=11)
    s.text(xs[1] + 56, y + 17, w - 76, 24, "Per-version release verdict",
           size=14, bold=True, color=AMBER, check="gh2")
    s.text(xs[1] + 56, y + 41, w - 76, 16, "advisory · returns a record",
           size=9.5, bold=True, color=AMBER, caps=True)
    s.text(xs[1] + 21, y + 66, w - 42, 74,
           "Filed as evidence; it blocks nothing. Because it is advisory, an urgent "
           "release is never blocked by a stale artifact — and the override path that "
           "would otherwise erode the enforced gate never needs to exist at all.",
           size=10.0, color=AMBER_INK, spacing=1.28, check="gb2")
    y += 164

    y = s.label(ML, y, CW, "The argument, and its price")
    y = card_grid(
        s,
        [("Why not enforce on release",
          "A blocking gate must be overridable. The override becomes the normal path, "
          "and the gate stops carrying information."),
         ("What it costs",
          "“Gated” names two mechanisms and must be stated carefully every time — "
          "including in this talk."),
         ("What it is not",
          "Not a system-wide interlock. The direct operator promotion endpoint can "
          "carry an advisory verdict and an attributed override without entering the "
          "refinement state machine.")],
        ML, y, CW, cols=3, gap=14, bottom=FLOOR,
    )


def s_live_targets(deck: Deck) -> None:
    s = deck.slide(notes=(
        "R2 in practice, and the capability that most distinguishes CALIBER from a "
        "versioned prompt file. Be careful with the fourth card: the approver scope "
        "exists but nothing requires the applier to differ from the author, so this "
        "is not separation of duties and we no longer describe it as one."
    ))
    y = s.head(
        "Capabilities  ·  §7.1–7.2  ·  R2 and R3",
        "Late binding: the client resolves which version to run",
        "An agent loads @prod; the platform decides what @prod points to. A "
        "remediation therefore requires no client deployment.",
    )
    y = card_grid(
        s,
        [("Remediation decouples from deployment",
          "The bound on time-to-fix becomes the refinement path plus operator "
          "attention, not the CI pipeline. Appendix C specifies how to measure this. "
          "We have not measured it."),
         ("Rollback is a pointer move",
          "A bounded database and registry operation, and the target restored is the "
          "one the release recorded — not one inferred as “the previous number.”"),
         ("Authoring cannot move the pointer",
          "Registering a version is non-live: the shared registration helper rejects "
          "a request that would also set an alias and directs the caller to the "
          "release service."),
         ("Authority is scope-checked",
          "Rotating an alias requires the operator or admin scope. This is not "
          "separation of duties: nothing requires the applier to differ from the "
          "author, and we no longer call it that.")],
        ML, y, CW, tone="teal", cols=4, gap=13,
        glyph=["1", "2", "3", "4"], bottom=302,
    ) + 14

    xs, w = columns(2, 16)
    s.card(xs[0], y, w, 154, "plain")
    s.text(xs[0] + 19, y + 14, w - 38, 20, "R3: evidence bound to versions",
           size=12.5, bold=True, color=INK, check="r3h")
    s.text(xs[0] + 19, y + 44, w - 38, 96,
           "A score in a report tells you a number; a score attached to a version tells "
           "you what you knew when you shipped. CALIBER binds per-dimension scores, the "
           "corpus identity and version, the diagnosis, the selected optimizer, and the "
           "gate decision to the candidate, and mints a provenance anchor at release.",
           size=10.0, color=MUTED, spacing=1.26, check="r3b")

    s.card(xs[1], y, w, 154, "warm")
    s.text(xs[1] + 19, y + 14, w - 38, 20, "The caveat indirection carries",
           size=12.5, bold=True, color=WARM, check="cvh")
    s.text(xs[1] + 19, y + 44, w - 38, 96,
           "A live target is mutable global state that determines production behaviour, "
           "so its integrity becomes critical. A rotation on the governed path is "
           "scope-checked, audited and checkpointed — but that answer does not yet "
           "cover every route that can perform one.",
           size=10.0, color=WARM_INK, spacing=1.26, check="cvb")


def s_release_path(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Algorithm 2. The honest framing is that a durable intent is not distributed "
        "atomicity: the provider call and the SQL settlement still cannot commit "
        "together. What changed is that divergence became observable rather than "
        "silent — and someone still has to operate the reconciler."
    ))
    y = s.head(
        "The release path  ·  §6 and §9  ·  Algorithm 2",
        "Intent-first release across a boundary no transaction spans",
        "Object-storage writes and external registry effects cannot join the caller's "
        "SQL transaction. Naming the two dual-write boundaries is what allows a precise "
        "audit guarantee to be stated at all.",
    )
    steps = [
        ("1", "Durable intent", "The exact before and after versions are written and "
                                "committed before the provider is called."),
        ("2", "Idempotent operation", "The provider call is safe to repeat, so a retry "
                                      "after an ambiguous failure is not a second effect."),
        ("3", "Observable state", "A crash after alias assignment leaves the operation "
                                  "in applying, visible through the release-operations "
                                  "endpoint."),
        ("4", "Reconciliation", "The reconciler observes the provider and settles "
                                "applied, failed, or reconcile_required."),
    ]
    xs, w = columns(4, 13)
    for i, (num, head, body) in enumerate(steps):
        s.card(xs[i], y, w, 140, "teal")
        s.badge(xs[i] + 17, y + 16, 24, num, TEAL, size=10.5)
        s.text(xs[i] + 49, y + 17, w - 68, 22, head, size=12.0, bold=True,
               color=TEAL, check="sth")
        s.text(xs[i] + 18, y + 52, w - 36, 74, body, size=10.0, color=INK,
               spacing=1.26, check="stb")
        if i < 3:
            s.text(xs[i] + w, y + 61, 13, 18, "›", size=13, bold=True,
                   color=TEAL_BRIGHT, align="c")
    y += 152

    y = card_grid(
        s,
        [("What this establishes",
          "Prompt creation and version authoring are non-live, and every in-repository "
          "prompt-alias call site uses the intent-first service. A durable "
          "reconciliation obligation exists before a prompt alias changes."),
         ("What it does not establish",
          "It does not prove that workflow, skill, knowledge-base, or future family "
          "effects have the same ordering. Their guarantees remain the ones in Table 3. "
          "The claim is path- and family-scoped, not universal."),
         ("What remains impossible",
          "A durable intent is not distributed atomicity. The provider call and the SQL "
          "settlement still cannot commit together. This removes silent divergence; it "
          "does not remove the need to operate the reconciler.")],
        ML, y, CW, cols=3, gap=14, bottom=452,
    ) + 14
    s.text(ML, y, CW, 18,
           "Nor does a repository call-site inventory prevent an external administrator "
           "from changing an MLflow alias outside CALIBER.",
           size=10.5, bold=True, color=MUTED, check="close")


def s_loops(deck: Deck) -> None:
    s = deck.slide(notes=(
        "This table exists because an earlier draft told one story about “the queue” "
        "and a reviewer checked it against five different loops. The claim predicate "
        "is shared and gives concurrent mutual exclusion for all of them; recovery "
        "after the owner dies is not shared, and recovery is what decides delivery "
        "semantics."
    ))
    y = s.head(
        "Execution  ·  §5.6  ·  Table 4  ·  Algorithm 1",
        f"{S['statLoopsC']} loops, and no single delivery guarantee",
        "The conditional claim predicate is shared and establishes concurrent mutual "
        "exclusion for all of them. Recovery after the owner dies is not shared — and "
        "that is what determines delivery semantics.",
    )
    rows = [
        ["Refinement", "heartbeat",
         "janitor marks the job failed after a stale heartbeat; it is not requeued",
         "at-most-once; a crash loses progress and needs operator action"],
        ["WorkflowRun", "lease + heartbeat",
         "lease expiry requeues the run, which resumes from its last checkpoint",
         "at-least-once at the step boundary; the effect ledger suppresses repeats"],
        ["CalibrationDrain", [("none", WARM, True)],
         "none automatic: a row stays running until an operator abandons it or creates "
         "a lineage-linked retry",
         "at-most-once per job; retries are explicit new jobs"],
        ["KnowledgeBase", "claim",
         "re-ingest is idempotent per source, so a repeat is absorbed",
         "at-least-once, idempotent"],
        ["WebhookDispatcher", "claim",
         "redelivery with settlement and a dead-letter path",
         "at-least-once, by design"],
        ["WorkflowScheduler", [("none", WARM, True)],
         "idempotent by a minute-bucketed key behind a unique partial index",
         "exactly-once firing; the work it enqueues then follows WorkflowRun"],
        ["Janitor", [("none", WARM, True)],
         "an idempotent sweep; repeating it changes nothing", "idempotent"],
        ["AriaPlan", [("none", WARM, True)],
         "polls for plans parked on settled jobs; no claim is taken",
         "at-least-once resume"],
    ]
    y = s.table(ML, y, [140, 100, 330, 289],
                ["Loop", "Lease", "Recovery after owner death",
                 "Delivery semantics"], rows, size=8.5, bottom=386)
    note(s, y + 14,
         "External effects are a separate question from job delivery",
         "The workflow effect ledger records an in_progress external call whose outcome "
         "after a crash is indeterminate — neither confirmed nor safely repeatable — and "
         "surfaces it as such rather than guessing.",
         "amber")


def s_evaluation(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Say this before the methodology, because a reader is entitled to know it "
        "first. The performance, scale, comparison and human-subject measurements have "
        "not been executed. The empty table is a choice: a plausible wrong number "
        "cannot be unfilled, and it propagates into citations."
    ))
    y = s.head(
        "Evaluation  ·  §10  ·  Table 5",
        "The quantitative evaluation is specified and unrun",
        "This is the paper's primary limitation and it is not mitigated by anything "
        "else in it. Every quantitative cell renders as TBM rather than as an estimate.",
    )
    s.card(ML, y, CW, 60, "warm")
    s.badge(ML + 20, y + 18, 26, "!", WARM, size=11)
    s.text(ML + 58, y + 15, CW - 80, 36,
           "An empty cell is honest and can be filled; a plausible wrong number cannot "
           "be unfilled. Numbers that were projected, carried over from an earlier "
           "configuration, or produced under undisclosed conditions are indistinguishable "
           "from measured ones to a reader, and they propagate into citations.",
           size=11.0, color=WARM_INK, spacing=1.28, check="warn")
    y += 72

    y = s.label(ML, y, CW,
                "What does execute: eight deterministic structural checks")
    checks = ["Conditional queue ownership", "Operator fencing of late results",
              "Release intent ordering", "Reconciliation settlement",
              "Prepared release abandonment", "Resolver outage fallback",
              "Synchronous publication gating", "Conflicting-select interleaving"]
    xs, w = columns(4, 11)
    for i, c in enumerate(checks):
        cy = y + (i // 4) * 46
        s.card(xs[i % 4], cy, w, 38, "teal")
        s.text(xs[i % 4] + 12, cy + 12, w - 24, 16, c, size=9.5, color=TEAL,
               bold=True, check="check")
    y += 100

    y = s.label(ML, y, CW,
                "Five question groups, each able to falsify a claim made earlier",
                color=MUTED)
    groups = [
        ("E1", "Control-plane cost", "falsified by a resolution cost large enough that "
                                     "teams would cache around it"),
        ("E2", "Queue arbitration", "falsified by two workers holding one row, or a "
                                    "loop whose recovery differs from Table 4"),
        ("E3", "The refinement path", "the gate's agreement with expert judgement, and "
                                      "whether authored judges track humans"),
        ("E4", "Scale and fault behaviour", "replica-scale stress and the fault-injection "
                                            "matrix specified in Appendix C"),
        ("E5", "Reviewability", "a human study CALIBER's central value claim needs and "
                                "this paper does not have"),
    ]
    xs, w = columns(5, 11)
    gh = min(FLOOR - y, 118.0)
    for i, (tag, head, body) in enumerate(groups):
        s.card(xs[i], y, w, gh, "plain")
        s.text(xs[i] + 13, y + 12, 26, 15, tag, size=10.0, bold=True,
               color=MUTED)
        s.text(xs[i] + 13, y + 29, w - 26, 26, head, size=10.0, bold=True,
               color=INK, spacing=1.10, check="ghead")
        s.text(xs[i] + 13, y + 60, w - 26, gh - 74, body, size=8.5,
               color=MUTED, spacing=1.24, check="gbody")
    check_floor(s.number, y + gh, "groups")


def s_comparison(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Three differences survive, and they are narrower than the old framing. Then "
        "give the losses — a comparison without a cost column is a marketing artifact. "
        "Be explicit that a reviewer who finds this delta insufficient is making a "
        "reasonable judgement about significance rather than catching an error."
    ))
    y = s.head(
        "Comparison  ·  §11.1 and §11.4",
        "Three differences survive, and three costs come with them",
        "CALIBER's claim is not that it does more than these systems, and not that the "
        "release path is unoccupied — for prompts it is occupied, by at least three "
        "products, with mechanisms that closely resemble CALIBER's.",
    )
    y = card_grid(
        s,
        [("Family heterogeneity",
          f"These platforms govern prompts, and MLflow also governs models. Workflows, "
          f"tool definitions, MCP server configurations and retrieval corpora are not "
          f"registry citizens with versions and pointers. One mode vocabulary spans "
          f"{S['statFamiliesW']} families — and spanning them costs uniform guarantees."),
         ("Evidence as a precondition",
          "All three platforms link evaluations to prompts. None of their documentation "
          "describes a mechanism that makes a promotion fail because a score fell. The "
          "advancement gate is such a mechanism — inside the refinement state machine, "
          "and only there."),
         ("The wiring, not the parts",
          "MLflow optimizes prompts and MLflow versions prompts, but the path from a "
          "flagged production trace to an optimizer run to a gated candidate to a "
          "reviewed release is assembled by the adopter. The claim is that this path is "
          "worth being a first-class architectural object.")],
        ML, y, CW, tone="teal", cols=3, gap=14, glyph=["1", "2", "3"],
    ) + 14

    y = card_grid(
        s,
        [("Managed operation",
          "Every observability platform in the table offers a hosted option. CALIBER "
          "must be run by the adopting team, on their own PostgreSQL, object storage "
          "and MLflow. For a small team this may outweigh every architectural "
          "consideration in the paper."),
         ("Uniformity",
          "An adopter who wants one release contract across all artifact types will "
          "find per-family guarantees unsatisfying. Understanding CALIBER requires "
          "reading Table 3 rather than one sentence, and that is a real burden."),
         ("Construction",
          "If the task is to build an agent, a composition framework is the right tool "
          "and CALIBER is not one. Agents are composed elsewhere and governed here; "
          "these systems are not substitutes.")],
        ML, y, CW, tone="warm", cols=3, gap=14, glyph="✕", bottom=FLOOR,
    )


def s_decisions(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Every row has a cost cell. A design-decisions table without one is a list of "
        "features. Several of these trades are contestable, and a reader should be "
        "able to disagree with a specific row rather than with the system."
    ))
    y = s.head(
        "Design decisions  ·  §8  ·  Table 2",
        "The load-bearing decisions, each with the price it charges",
        "Recording the cost is the point. §12 is largely the last column read back.",
    )
    rows = [
        ["D1", "Reuse MLflow for traces, prompt versions and evaluation",
         "rebuilding tracking competes with a mature system for no governance gain, "
         "and would force a trace migration to adopt CALIBER",
         "two stores with no shared transaction, so a reconciliation boundary is "
         "unavoidable"],
        ["D2", "Per-family guarantees rather than one uniform contract",
         "a test set and a prompt are not the same kind of thing; a uniform contract "
         "is vacuous or excludes useful families",
         "the guarantee surface must be read per row and cannot be summarized"],
        ["D3", "Enforce the gate on candidate advancement; keep the release verdict "
               "advisory",
         "a blocking gate must be overridable, the override becomes the normal path, "
         "and the gate stops carrying information",
         "“gated” names two mechanisms and must be stated carefully every time"],
        ["D4", "A human decision at refinement Apply, always",
         "score gains on a corpus assembled from past failures are necessary but not "
         "sufficient, and auto-apply makes a reviewable change invisible",
         "refinement throughput is bounded by operator attention"],
        ["D5", "In-process loops with database-arbitrated queues; no worker tier",
         "claim and lease columns give durable arbitration with one deployable and no "
         "split-brain between broker and database",
         "loop capacity is coupled to request capacity; in-memory limiters become "
         "per-process"],
        ["D8", "Containment for local tool and MCP execution; attestation only for "
               "production",
         "requiring a sandbox runtime locally would make the common path unusable, and "
         "naming the weaker guarantee beats overclaiming",
         "the shipped runner keeps ambient host authority; untrusted authors need an "
         "operator-supplied backend"],
        ["D9", "Ship a single environment in v1",
         "a dev→staging→prod ladder needs approval routing, requester/approver "
         "separation and per-stage config; a half-built one is a false assurance",
         "no staging rehearsal, and a future multi-environment mode must rebuild the "
         "pending-approval path"],
    ]
    y = s.table(ML, y, [44, 235, 300, 280],
                ["", "Decision", "Why this one", "What it costs"],
                rows, size=8.0, pad=6.0, gutter=8.0, bottom=FLOOR)


def s_limitations(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Grouped by kind, because they demand different responses: an unrun experiment "
        "is a schedule problem, an unenforced boundary is a security problem, and a "
        "per-family guarantee surface is a design consequence. Three of these were "
        "found by checking the paper's claims against the implementation."
    ))
    y = s.head(
        "Limitations  ·  §12",
        "Six results this work deliberately does not deliver",
        "Grouped by kind, because they demand different responses.",
    )
    y = card_grid(
        s,
        [("The evaluation has not been run",
          "Claims about control-plane overhead, drain throughput, gate agreement and "
          "rollback latency are unsupported in this draft — not weakly supported. "
          "Treat the architectural argument as the contribution and the performance "
          "characterization as future work."),
         ("No user study",
          "The central value claim is that a release becomes reviewable. Whether "
          "operators actually review more effectively, or merely click through a "
          "richer interface, is an empirical question about people that no amount of "
          "systems measurement answers."),
         ("Single-system evidence",
          "Every architectural claim is grounded in one implementation. We argue that "
          "per-family guarantees follow from the heterogeneity of the artifact types, "
          "but a single system cannot establish that a different factoring is "
          "impossible."),
         ("Apply is a confirmation, not an approval",
          "The route mints an approval record already in the approved state, with the "
          "acting operator recorded as the approver. A sibling approver scope exists, "
          "but nothing requires the applier to differ from the author. The "
          "Clark–Wilson property is the target, not the present state."),
         ("Two dual-write boundaries cannot be closed",
          "Object-storage writes and external registry effects cannot join the caller's "
          "SQL transaction. Prompt-alias writes are intent-first and idempotently "
          "reconciled, which makes divergence observable but not atomic. A failed "
          "object write can still leave orphaned bytes."),
         ("Containment, not isolation",
          "The shipped local runner for tools and MCP keeps ambient host authority. "
          "We are careful about which word applies throughout: distinguishing "
          "containment from isolation is what keeps the security claim falsifiable.")],
        ML, y, CW, tone="warm", cols=3, gap=14, rgap=14, glyph="✕",
        bottom=440,
    ) + 14
    s.text(ML, y, CW, 30,
           "The most likely operator error in CALIBER is assuming a guarantee transfers "
           "across families because the same interface component is mounted in both "
           "places. The shared version panel is the canonical trap.",
           size=10.5, bold=True, color=MUTED, check="close")


def s_register(deck: Deck) -> None:
    s = deck.slide(notes=(
        "The register exists to prevent narrative drift between the talk and the "
        "manuscript. Read the standing column honestly — three of these rows are "
        "arguments, not results, and one is explicitly unmet."
    ))
    y = s.head(
        "The register  ·  §1, §5.1, §10, §12",
        "Every claim, its evidence, and its standing",
        "A claim whose standing is not stated is a claim a reader has to reconstruct. "
        "This is the register the rest of the talk reduces to.",
    )
    def standing(word, color, phrase):
        return [(word, color, True), (" — " + phrase, MUTED, False)]
    rows = [
        ["C1", "The governed asset is a coherent unit of governance across families",
         f"{S['statFamiliesW']} families wired against one base contract; Table 3",
         standing("established", TEAL, "this implementation")],
        ["C2", "Adjacency confers availability, not inheritance",
         "the prompt / test set / judge argument, plus the per-family wiring",
         standing("established", TEAL, "and the reason is intrinsic")],
        ["C3", "Uniform obligations are possible, but not present",
         "capability interfaces are the natural design and were not taken",
         standing("unmet", WARM, "documentation-only enforcement")],
        ["C4", "An enforced gate can precede a human decision without being routed around",
         "the advancement gate inside the refinement state machine",
         standing("argued", AMBER, "scoped to that machine")],
        ["C5", "A release is reconstructable from records months later",
         "durable residue at each chain term; provenance anchor at release",
         standing("argued", AMBER, "no user study")],
        ["C6", "Prompt-alias release is crash-observable",
         "durable intent, idempotent provider operation, reconciliation state",
         standing("established", TEAL, "path- and family-scoped")],
        ["C7", "Control-plane indirection is cheap on the serving path",
         "E1 in Appendix C specifies the measurement",
         standing("unmet", WARM, "not yet run")],
        ["C8", "Governed releases are more reviewable in practice",
         "E5 would require a human study",
         standing("unmet", WARM, "not yet run")],
    ]
    y = s.table(ML, y, [44, 300, 300, 215],
                ["", "Claim", "Direct evidence", "Standing"],
                rows, size=8.0, pad=6.5, gutter=8.0, bottom=396)
    note(s, y + 14,
         "Why the register is on a slide at all",
         "Three of these rows are arguments rather than results and two are explicitly "
         "unmet. Stating that in one place is what stops a talk from drifting away from "
         "the manuscript it is about.",
         "neutral")


def s_future(deck: Deck) -> None:
    s = deck.slide(notes=(
        "Four next steps, in the order they would change what the paper can claim. "
        "The first two convert arguments into results; the third converts documented "
        "obligations into enforced ones; the fourth is the open positioning question."
    ))
    y = s.head(
        "Future work  ·  §13",
        "What would change the standing of these claims",
        "Ordered by how much each one would move the register, not by how much work "
        "it is.",
    )
    y = card_grid(
        s,
        [("Run the protocol",
          "Appendix C specifies the questions, the baselines, the fault-injection "
          "matrix, and the analysis in enough detail to be executed by someone else. "
          "Executing it converts E1–E4 from specification to result and fills Table 5."),
         ("Run the human study",
          "E5 is the one question no systems measurement answers. Reviewability is "
          "CALIBER's central value claim, and it is currently argued rather than "
          "demonstrated."),
         ("Retrofit capability interfaces",
          "A thin base contract plus Releasable, Rollbackable and EvidenceBearing, "
          "checked by the type system or a start-up registry projection. This converts "
          "the guarantee surface from documented to enforced."),
         ("Settle the absorption question",
          "MLflow already has the registry, the aliases, the evaluation harness and the "
          "optimizer. What it lacks is a second artifact type and a gate. Whether this "
          "warrants a separate control plane is a legitimate open question.")],
        ML, y, CW, tone="teal", cols=4, gap=13, glyph=["1", "2", "3", "4"],
        bottom=336,
    ) + 14
    note(s, y,
         "The honest reading of the last card",
         "The evidence currently tilts toward absorption being feasible. An absorbing "
         "system would need a typed multi-family registry, an authority model that "
         "distinguishes authoring from promoting, and an enforced precondition on the "
         "pointer move. That is a smaller distance than an earlier draft of this paper "
         "implied, and saying so is worth more than defending the perimeter.",
         "amber", bottom=FLOOR)


def s_conclusion(deck: Deck) -> None:
    s = deck.slide(NAVY, notes=(
        "Close on the capability boundary — it is the result most worth carrying "
        "forward and it is independent of whether CALIBER itself is the right vehicle. "
        "Then restate what is not established, in the same breath."
    ))
    s.ellipse(885, 381, 316, PANEL)
    s.ellipse(943, 439, 158, TEAL)

    s.text(ML, EYEBROW_Y, CW, 18, "Conclusion  ·  §14", size=10.5, bold=True,
           color=TEAL_BRIGHT, caps=True)
    s.text(ML, TITLE_Y, 806, 66,
           "A control plane should be judged by how precisely it can say where its "
           "guarantees stop",
           size=25, bold=True, color=WHITE, spacing=1.14, check="title")

    points = [
        ("1", "The capability boundary is the result",
         "Adjacency in a layered stack confers capability availability, not "
         "inheritance. A meaningful base contract can still be uniform — identity, "
         "history, authority, audit — while release, rollback, evaluation and evidence "
         "remain declared capabilities."),
        ("2", "The problem is fragmentation, not absence",
         "Prompt platforms now provide real versioning, live labels, rollback and "
         "permissions. The remaining problem is not an empty release ecosystem; it is "
         "one with different guarantees by resource family."),
        ("3", "Naming a boundary is what makes a claim checkable",
         "Naming the two dual-write boundaries is what allows a precise audit guarantee "
         "to be stated at all, and distinguishing containment from isolation is what "
         "keeps a security claim falsifiable."),
        ("4", "The evaluation is unrun, and that is stated as such",
         "Table 5 is empty by choice rather than by omission. There is no user study, "
         "so reviewability is argued rather than demonstrated, and the evidence is from "
         "one implementation."),
    ]
    xs, w = columns(2, 26, 806, ML)
    for i, (num, head, body) in enumerate(points):
        px, py = xs[i % 2], 150 + (i // 2) * 150
        hh = measure(head, w - 43, 14, bold=True, spacing=1.16)
        s.ellipse(px, py, 30, TEAL)
        s.text(px, py + 6, 30, 20, num, size=13, bold=True, color=WHITE,
               align="c")
        s.text(px + 43, py - 2, w - 43, hh, head, size=14, bold=True,
               color=WHITE, spacing=1.16, check="chead")
        s.text(px + 43, py - 2 + hh + 8, w - 43, 92, body, size=10.5,
               color=TEAL_PALE, spacing=1.32, check="cbody")

    s.rule(ML, 446, 806, RULE_DARK)
    s.text(ML, 456, 806, 30,
           "What the architecture establishes is narrower, and we hope more durable: "
           "that the release path for non-code agent resources is a coherent "
           "first-class concern, and that being precise about where the guarantees "
           "stop is what separates a control plane from a dashboard with opinions.",
           size=11, color=MUTED_DARK, spacing=1.30, check="closing")


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def build() -> int:
    deck = Deck(title="CALIBER", footer=FOOTER)

    title_slide(deck)
    s_motivation_steps(deck)
    s_obvious_answers(deck)
    s_positioning(deck)

    divider(deck, "I", "The abstraction",
            "The governed asset, six lifecycle modes, the governance chain, and the "
            f"{S['statLayersW']}-layer factoring that carries them.",
            notes="Movement one is structure. Nothing here is contested; it is the "
                  "vocabulary the second movement argues about.")
    s_asset(deck)
    s_modes(deck)
    s_chain(deck)
    s_layers(deck)

    divider(deck, "II", "Per-family governance",
            "Availability is not inheritance. What one mode vocabulary costs when it "
            f"spans {S['statFamiliesW']} genuinely heterogeneous families.",
            notes="Movement two is the paper's actual intellectual content. If the "
                  "audience remembers one slide, it should be the central claim.")
    s_claim(deck)
    s_families(deck)
    s_third_design(deck)
    s_gates(deck)
    s_live_targets(deck)
    s_release_path(deck)
    s_loops(deck)

    divider(deck, "III", "Evidence and limits",
            "What is measured, what is merely specified, what the comparison costs, "
            "and what this work does not establish.",
            notes="Movement three is where the deck has to be hardest on itself. Lead "
                  "with the unrun evaluation rather than burying it.")
    s_evaluation(deck)
    s_comparison(deck)
    s_decisions(deck)
    s_limitations(deck)
    s_register(deck)
    s_future(deck)

    s_conclusion(deck)

    n = deck.save(str(OUT))
    return n


if __name__ == "__main__":
    count = build()
    print(f"wrote {OUT.relative_to(PAPER.parent)}  ({count} slides)")
