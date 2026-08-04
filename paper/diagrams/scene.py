"""The scene DSL, and the emitter that turns a scene into a real Excalidraw file.

A scene is a list of elements in Excalidraw's own schema, so the output of
``Scene.to_excalidraw()`` opens, edits, and re-exports at excalidraw.com like any
hand-drawn document. The DSL exists so that the *source* of a diagram is reviewable
Python rather than a JSON blob of random element IDs.

Coordinates are in **points at final printed size**, with y increasing downward.
That choice is deliberate: it makes the type scale directly checkable against the
paper's 7pt figure floor, because a scene placed at ``width=<Scene.width>pt`` needs
no scaling at all. Authoring in arbitrary pixels and scaling to fit is how the first
version of these diagrams ended up with 5pt labels.

Excalidraw itself is unit-agnostic, so the emitted ``.excalidraw`` file opens at a
comfortable size and edits normally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from metrics import text_width
from palette import INK, TRANSPARENT, role

# --------------------------------------------------------------------- style ---
# One place for the knobs that decide how hand-drawn this looks.
#
# Excalidraw's "sloppiness" control maps onto roughjs roughness: architect = 0,
# artist = 1, cartoonist = 2. The paper targets a systems conference, where the
# default sketchy look reads as informal and undercuts an argument about precision,
# so these sit just above architect: enough character to be recognisably
# Excalidraw, crisp enough for review. Raise ROUGHNESS toward 1.5 for a blog or a
# slide deck.
STYLE = {
    "roughness": 0.5,
    "bowing": 0.5,
    "strokeWidth": 0.62,
    "boldStrokeWidth": 1.0,
    "fillStyle": "solid",  # Excalidraw's cleanest fill; hachure reads as a sketch
    "fontFamily": 2,  # 1 = hand-drawn (Excalifont), 2 = Normal, 3 = Code
    "roundness": 3,  # Excalidraw's adaptive corner radius
}

FONT_STACK = "Nunito, Helvetica, Arial, sans-serif"

# Type scale in points at final size. FLOOR is the same 7pt the TikZ figures hold:
# below it, figure text is unreadable in print and in the two-page-per-sheet
# printouts many reviewers use. Nothing in a scene may be set smaller.
FLOOR = 7.0
SIZE_HEAD = 8.0    # box titles
SIZE_BODY = 7.0    # detail lines
SIZE_SMALL = 7.0   # captions inside the figure, edge labels

LINE_HEIGHT = 1.28


def _seeded(index: int) -> int:
    """A stable roughjs seed.

    Derived from the element's position in the scene rather than randomly, so the
    rendered SVG is byte-identical across machines and runs. A build that is not
    reproducible cannot be checked.
    """
    return (index * 2654435761) % 2147483647


@dataclass
class Scene:
    """A diagram: a list of Excalidraw elements plus the box they occupy."""

    name: str
    width: float
    height: float
    elements: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    # ----------------------------------------------------------- primitives ---
    def _next_id(self, kind: str) -> tuple[str, int]:
        self._seq += 1
        return f"{self.name}-{kind}-{self._seq}", self._seq

    def _base(self, kind: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
        el_id, seq = self._next_id(kind)
        return {
            "id": el_id,
            "type": kind,
            "x": round(x, 2),
            "y": round(y, 2),
            "width": round(w, 2),
            "height": round(h, 2),
            "angle": 0,
            "strokeColor": INK,
            "backgroundColor": TRANSPARENT,
            "fillStyle": STYLE["fillStyle"],
            "strokeWidth": STYLE["strokeWidth"],
            "strokeStyle": "solid",
            "roughness": STYLE["roughness"],
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": None,
            "seed": _seeded(seq),
            "version": 1,
            "versionNonce": _seeded(seq + 7),
            "isDeleted": False,
            "boundElements": None,
            "updated": 1,
            "link": None,
            "locked": False,
        }

    # ------------------------------------------------------------------ box ---
    def box(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        role_name: str = "muted",
        dashed: bool = False,
        bold: bool = False,
        filled: bool = True,
        rounded: bool = True,
    ) -> dict[str, Any]:
        """A rounded rectangle in one of the palette roles."""
        r = role(role_name)
        el = self._base("rectangle", x, y, w, h)
        el["strokeColor"] = r.stroke
        el["backgroundColor"] = r.fill if filled else TRANSPARENT
        el["strokeStyle"] = "dashed" if dashed else "solid"
        el["strokeWidth"] = STYLE["boldStrokeWidth"] if bold else STYLE["strokeWidth"]
        el["roundness"] = {"type": STYLE["roundness"]} if rounded else None
        self.elements.append(el)
        return el

    def band(self, x: float, y: float, w: float, h: float, *, dashed: bool = False):
        """A container outline: no fill, hairline rule, generous corners."""
        el = self._base("rectangle", x, y, w, h)
        el["strokeColor"] = role("muted").stroke
        el["backgroundColor"] = TRANSPARENT
        el["strokeWidth"] = 1.0
        el["strokeStyle"] = "dashed" if dashed else "solid"
        el["roundness"] = {"type": STYLE["roundness"]}
        el["opacity"] = 70
        self.elements.append(el)
        return el

    # ----------------------------------------------------------------- text ---
    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float = SIZE_BODY,
        colour: str = INK,
        align: Literal["left", "center", "right"] = "center",
        bold: bool = False,
        italic: bool = False,
        width: float | None = None,
    ) -> dict[str, Any]:
        """A text run. ``content`` may contain newlines.

        ``x`` is the anchor: the left edge, centre, or right edge according to
        ``align``. ``y`` is the top of the first line. Widths are estimated only
        for the ``.excalidraw`` file, which re-measures on load; the SVG renderer
        anchors text instead of measuring it, so an estimate is harmless.
        """
        if size < FLOOR - 1e-9:
            raise ValueError(
                f"{self.name}: {size}pt text is below the {FLOOR}pt floor: "
                f"{content.splitlines()[0]!r}"
            )
        lines = content.split("\n")
        est_w = width if width is not None else max(
            text_width(line, size, bold=bold, italic=italic) for line in lines
        )
        est_h = len(lines) * size * LINE_HEIGHT
        el = self._base("text", x if align == "left" else x - est_w / 2, y, est_w, est_h)
        el["strokeColor"] = colour
        el["text"] = content
        el["originalText"] = content
        el["fontSize"] = size
        el["fontFamily"] = STYLE["fontFamily"]
        el["textAlign"] = align
        el["verticalAlign"] = "top"
        el["lineHeight"] = LINE_HEIGHT
        el["containerId"] = None
        el["autoResize"] = True
        # Non-Excalidraw hints the renderer uses and Excalidraw ignores.
        el["_anchorX"] = round(x, 2)
        el["_bold"] = bold
        el["_italic"] = italic
        self.elements.append(el)
        return el

    # Padding a labelled box keeps between its text and its border.
    BOX_PAD = 4.0
    BOX_PAD_X = 3.5
    TITLE_GAP = 1.8

    @classmethod
    def block_height(
        cls,
        title: str,
        detail: str = "",
        *,
        title_size: float = SIZE_HEAD,
        detail_size: float = SIZE_BODY,
        padded: bool = True,
    ) -> float:
        """The height a title-plus-detail block needs.

        Boxes are sized from this rather than by eye. Guessing a height is the one
        mistake in a generated diagram that no amount of care prevents and that only
        shows up as text spilling past a border in the rendered output.
        """
        n_title = len(title.split("\n"))
        n_detail = len(detail.split("\n")) if detail else 0
        total = (
            n_title * title_size * LINE_HEIGHT
            + (cls.TITLE_GAP if detail else 0.0)
            + n_detail * detail_size * LINE_HEIGHT
        )
        return total + (2 * cls.BOX_PAD if padded else 0.0)

    def label_in(
        self,
        b: dict[str, Any],
        title: str,
        detail: str = "",
        *,
        title_size: float = SIZE_HEAD,
        detail_size: float = SIZE_BODY,
        colour: str = INK,
    ) -> None:
        """Centre a title, and an optional detail block, inside a box.

        Raises if the text does not fit. A silent overflow is exactly the defect
        this whole generated-diagram approach is supposed to remove, so it fails
        the build instead of reaching the page.
        """
        needed = self.block_height(
            title, detail, title_size=title_size, detail_size=detail_size,
            padded=False,
        )
        if needed > b["height"] + 0.5:
            raise ValueError(
                f"{self.name}: text is too tall for {b['id']} "
                f"({needed:.1f}pt of content in a {b['height']:.1f}pt box). "
                f"Use labelled_box() to size it, or shorten: {title!r}"
            )
        # Width matters as much as height and is easier to get wrong, because a
        # too-narrow box silently pushes text past its border rather than wrapping.
        inner = b["width"] - 2 * self.BOX_PAD_X
        widest, widest_line = 0.0, ""
        for line, sz, bold in (
            [(ln, title_size, True) for ln in title.split("\n")]
            + [(ln, detail_size, False) for ln in detail.split("\n")]
        ):
            w = text_width(line, sz, bold=bold)
            if w > widest:
                widest, widest_line = w, line
        if widest > inner + 0.5:
            raise ValueError(
                f"{self.name}: text is too wide for {b['id']} "
                f"({widest:.1f}pt of text in a {inner:.1f}pt inner width). "
                f"Widen the box or break the line: {widest_line!r}"
            )
        n_title = len(title.split("\n"))
        total = needed
        cx = b["x"] + b["width"] / 2
        top = b["y"] + (b["height"] - total) / 2
        self.text(cx, top, title, size=title_size, bold=True, colour=colour)
        if detail:
            self.text(
                cx,
                top + n_title * title_size * LINE_HEIGHT + self.TITLE_GAP,
                detail,
                size=detail_size,
                colour=colour,
            )

    def labelled_box(
        self,
        x: float,
        y: float,
        w: float,
        title: str,
        detail: str = "",
        *,
        role_name: str = "muted",
        title_size: float = SIZE_HEAD,
        detail_size: float = SIZE_BODY,
        min_height: float = 0.0,
        dashed: bool = False,
        bold: bool = False,
    ) -> dict[str, Any]:
        """A box sized to its own content, then labelled. The preferred builder.

        Title and detail are wrapped to the box's inner width unless they already
        contain explicit newlines, in which case the author's line breaks are kept.
        Auto-wrapping is what makes these builders safe to write: the alternative is
        hand-placing every break and discovering the ones that were wrong only by
        looking at the rendered output.
        """
        inner = w - 2 * self.BOX_PAD_X
        if "\n" not in title:
            title = self.wrap(title, inner, title_size, bold=True)
        if detail and "\n" not in detail:
            detail = self.wrap(detail, inner, detail_size)
        h = max(
            min_height,
            self.block_height(
                title, detail, title_size=title_size, detail_size=detail_size
            ),
        )
        b = self.box(x, y, w, h, role_name=role_name, dashed=dashed, bold=bold)
        self.label_in(
            b, title, detail, title_size=title_size, detail_size=detail_size
        )
        return b

    @staticmethod
    def wrap(text: str, max_width: float, size: float, *, bold: bool = False,
             italic: bool = False) -> str:
        """Greedily wrap ``text`` to ``max_width`` points, inserting newlines.

        The scene builders need this because ``text_width`` measures but does not
        wrap: a label that does not fit its column runs past the canvas edge rather
        than breaking. Wrapping here, against the real metrics, is what makes a
        column width a constraint the layout actually respects.
        """
        words = text.split()
        if not words:
            return text
        lines: list[str] = []
        cur = words[0]
        for word in words[1:]:
            trial = f"{cur} {word}"
            if text_width(trial, size, bold=bold, italic=italic) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
        return "\n".join(lines)

    # ---------------------------------------------------------------- edges ---
    def arrow(
        self,
        points: list[tuple[float, float]],
        *,
        colour: str = INK,
        dashed: bool = False,
        bold: bool = False,
        both: bool = False,
        head: bool = True,
    ) -> dict[str, Any]:
        """A polyline arrow through ``points`` (absolute scene coordinates)."""
        x0, y0 = points[0]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        el = self._base(
            "arrow", x0, y0, max(xs) - min(xs), max(ys) - min(ys)
        )
        el["points"] = [[round(px - x0, 2), round(py - y0, 2)] for px, py in points]
        el["strokeColor"] = colour
        el["strokeStyle"] = "dashed" if dashed else "solid"
        el["strokeWidth"] = STYLE["boldStrokeWidth"] if bold else STYLE["strokeWidth"]
        el["roundness"] = {"type": 2}
        el["startArrowhead"] = "arrow" if both else None
        el["endArrowhead"] = "arrow" if head else None
        el["lastCommittedPoint"] = None
        el["startBinding"] = None
        el["endBinding"] = None
        el["elbowed"] = False
        self.elements.append(el)
        return el

    def line(
        self,
        points: list[tuple[float, float]],
        *,
        colour: str | None = None,
        dashed: bool = False,
        width: float = 1.0,
    ) -> dict[str, Any]:
        el = self.arrow(points, colour=colour or role("muted").stroke,
                        dashed=dashed, head=False)
        el["type"] = "line"
        el["strokeWidth"] = width
        return el

    # -------------------------------------------------------------- extents ---
    def content_bbox(self) -> tuple[float, float, float, float]:
        """The true bounding box of the drawn content.

        Arrows store ``y`` as their first point and ``height`` as the span of all
        points, so ``y + height`` overshoots any path that goes down and then back
        up. Walking the points instead is what keeps a scene from acquiring a band
        of empty space at the bottom.
        """
        xs: list[float] = []
        ys: list[float] = []
        for e in self.elements:
            if e["type"] in ("arrow", "line"):
                for dx, dy in e["points"]:
                    xs.append(e["x"] + dx)
                    ys.append(e["y"] + dy)
            elif e["type"] == "text":
                xs.extend([e["x"], e["x"] + e["width"]])
                ys.extend([e["y"], e["y"] + e["height"]])
            else:
                xs.extend([e["x"], e["x"] + e["width"]])
                ys.extend([e["y"], e["y"] + e["height"]])
        return min(xs), min(ys), max(xs), max(ys)

    def fit_height(self, margin: float = 8.0) -> None:
        """Set the canvas height from the content, plus a margin."""
        self.height = round(self.content_bbox()[3] + margin, 2)

    # ------------------------------------------------------------- emitters ---
    def to_excalidraw(self) -> str:
        """A real ``.excalidraw`` document: open it at excalidraw.com."""
        return json.dumps(
            {
                "type": "excalidraw",
                "version": 2,
                "source": "caliber-paper/diagrams (generated -- see paper/diagrams/README.md)",
                "elements": self.elements,
                "appState": {
                    "gridSize": None,
                    "viewBackgroundColor": "#ffffff",
                },
                "files": {},
            },
            indent=2,
        )

    def to_render_json(self) -> str:
        """The same scene plus the canvas box, for the SVG renderer."""
        return json.dumps(
            {
                "name": self.name,
                "width": self.width,
                "height": self.height,
                "fontStack": FONT_STACK,
                "elements": self.elements,
            },
            indent=1,
        )
