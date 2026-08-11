/*
 * CALIBER docs — shared client behaviour for the landing page and every
 * generated module page: theme toggle, Mermaid rendering, the DOCS_NAV-driven
 * sidebar, a per-page table of contents with scroll-spy, sidebar search, and
 * the quick-start tabs. Loaded with `defer` after `docs-nav.js`.
 */
(function () {
  "use strict";
  var docRoot = document.documentElement;

  var DOC_ICON_SVGS = {
    "compass": '<circle cx="12" cy="12" r="9"></circle><polygon points="15.5 8.5 13.5 13.5 8.5 15.5 10.5 10.5 15.5 8.5"></polygon>',
    "rocket": '<path d="M5 19c-1.2 1.2-1.4 3-1.4 3S5.4 21.8 6.6 20.6 8 17 8 17s-1.8.4-3 2z"></path><path d="M15 9 9 15"></path><path d="M14 4c2.3-1.1 4.9-1.1 7.2 0 1.1 2.3 1.1 4.9 0 7.2L17 15l-8-8z"></path>',
    "shield-check": '<path d="M12 3 5 7v5c0 4.7 3 7.6 7 9 4-1.4 7-4.3 7-9V7z"></path><path d="m9.5 12 1.8 1.8 3.7-3.8"></path>',
    "bot": '<path d="M12 3v3"></path><path d="M8 3h8"></path><rect x="4" y="7" width="16" height="12" rx="2"></rect><path d="M9 11h.01"></path><path d="M15 11h.01"></path><path d="M9 15h6"></path>',
    "eye": '<path d="M2.5 12s3.8-6 9.5-6 9.5 6 9.5 6-3.8 6-9.5 6-9.5-6-9.5-6z"></path><circle cx="12" cy="12" r="3"></circle>',
    "flask": '<path d="M10 2v6l-5 8a4 4 0 0 0 3.4 6h7.2A4 4 0 0 0 19 16l-5-8V2"></path><path d="M8 2h8"></path><path d="M7.5 14h9"></path>',
    "plug": '<path d="M9 3v4"></path><path d="M15 3v4"></path><path d="M8 7h8v4a4 4 0 0 1-4 4 4 4 0 0 1-4-4z"></path><path d="M12 15v6"></path>',
    "layers": '<path d="m12 3-9 5 9 5 9-5-9-5z"></path><path d="m3 12 9 5 9-5"></path><path d="m3 16 9 5 9-5"></path>',
    "gauge": '<path d="M4 14a8 8 0 1 1 16 0"></path><path d="m12 14 4-4"></path><path d="M12 14h.01"></path>',
    "book": '<path d="M3 6.5A2.5 2.5 0 0 1 5.5 4H11v16H5.5A2.5 2.5 0 0 0 3 22z"></path><path d="M21 6.5A2.5 2.5 0 0 0 18.5 4H13v16h5.5A2.5 2.5 0 0 1 21 22z"></path>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"></path><circle cx="9.5" cy="7" r="3"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 4.13a4 4 0 0 1 0 7.75"></path>',
    "sparkles": '<path d="m12 3 1.4 3.6L17 8l-3.6 1.4L12 13l-1.4-3.6L7 8l3.6-1.4z"></path><path d="m19 14 .9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9z"></path><path d="m5 15 .7 1.7L7.4 17l-1.7.7L5 19.4l-.7-1.7L2.6 17l1.7-.7z"></path>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path><path d="M8 9h8"></path><path d="M8 13h5"></path>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 0 0 5.4-5.4l-2.4 2.4-2.2-2.2z"></path>',
    "workflow": '<circle cx="6" cy="6" r="2"></circle><circle cx="18" cy="6" r="2"></circle><circle cx="18" cy="18" r="2"></circle><path d="M8 6h8"></path><path d="M8 6v10a2 2 0 0 0 2 2h6"></path>',
    "database": '<ellipse cx="12" cy="5" rx="7" ry="3"></ellipse><path d="M5 5v10c0 1.7 3.1 3 7 3s7-1.3 7-3V5"></path><path d="M5 10c0 1.7 3.1 3 7 3s7-1.3 7-3"></path>',
    "sliders": '<path d="M4 6h8"></path><path d="M16 6h4"></path><circle cx="14" cy="6" r="2"></circle><path d="M4 12h4"></path><path d="M12 12h8"></path><circle cx="10" cy="12" r="2"></circle><path d="M4 18h10"></path><path d="M18 18h2"></path><circle cx="16" cy="18" r="2"></circle>',
    "key": '<circle cx="7.5" cy="14.5" r="3.5"></circle><path d="M11 14.5h10"></path><path d="M18 11.5v6"></path><path d="M15 13.5v4"></path>',
    "code": '<polyline points="9 18 3 12 9 6"></polyline><polyline points="15 6 21 12 15 18"></polyline>',
    "settings": '<circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c0 .7.4 1.3 1 1.5h.2H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.4 1z"></path>',
    "activity": '<polyline points="3 12 7 12 10 7 14 17 17 12 21 12"></polyline>',
    "lifebuoy": '<circle cx="12" cy="12" r="8"></circle><circle cx="12" cy="12" r="3"></circle><path d="m8.5 8.5 7 7"></path><path d="m15.5 8.5-7 7"></path>',
    "target": '<circle cx="12" cy="12" r="8"></circle><circle cx="12" cy="12" r="4"></circle><path d="M12 2v3"></path><path d="M12 19v3"></path><path d="M2 12h3"></path><path d="M19 12h3"></path>',
    "route": '<circle cx="6" cy="18" r="2"></circle><circle cx="18" cy="6" r="2"></circle><path d="M8 18c5 0 2-8 8-8"></path><path d="M16 10V8h2"></path>',
    "bar": '<path d="M5 20V10"></path><path d="M12 20V4"></path><path d="M19 20v-7"></path>'
  };

  function sectionIdForTitle(title) {
    switch (String(title || "").toLowerCase()) {
      case "start here": return "start";
      case "use caliber": return "use";
      case "build & integrate": return "build";
      case "operate caliber": return "operate";
      case "examples": return "examples";
      case "reference": return "reference";
      case "architecture": return "architecture";
      case "strategy": return "strategy";
      default: return "";
    }
  }

  function iconKeyForSection(sectionIdOrTitle) {
    var sectionId = sectionIdOrTitle;
    if (!sectionId || sectionId.indexOf(" ") !== -1) sectionId = sectionIdForTitle(sectionIdOrTitle);
    switch (sectionId) {
      case "start": return "compass";
      case "use": return "workflow";
      case "build": return "code";
      case "operate": return "shield-check";
      case "examples": return "flask";
      case "reference": return "book";
      case "architecture": return "layers";
      case "strategy": return "target";
      default: return "compass";
    }
  }

  function iconKeyForPage(meta) {
    var href = meta && meta.href ? pageOf(meta.href) : "";
    var label = String(meta && meta.label || "").toLowerCase();
    if (href === "index.html") return "compass";
    if (href === "walkthrough.html") return "route";
    if (href === "interactive-layered-architecture.html") return "layers";
    if (href === "presentation.html" || href === "presentation_timed.html") return "target";
    if (href.indexOf("m-cookbook-") === 0 || label.indexOf("cookbook") !== -1 || label.indexOf("recipe") !== -1) return "flask";
    if (label.indexOf("quickstart") !== -1) return "rocket";
    if (label.indexOf("choose your") !== -1 || label.indexOf("path") !== -1) return "compass";
    if (label.indexOf("decision-maker") !== -1 || label.indexOf("competitive") !== -1 || label.indexOf("roadmap") !== -1) return "target";
    if (label.indexOf("prompt") !== -1) return "message";
    if (label.indexOf("tool") !== -1) return "wrench";
    if (label.indexOf("skill") !== -1) return "sparkles";
    if (label.indexOf("mcp") !== -1 || label.indexOf("gateway") !== -1) return "plug";
    if (label.indexOf("workflow") !== -1) return "workflow";
    if (label.indexOf("knowledge") !== -1 || label.indexOf("object store") !== -1 || label.indexOf("storage") !== -1) return "database";
    if (label.indexOf("evaluation") !== -1 || label.indexOf("test set") !== -1 || label.indexOf("judge") !== -1) return "flask";
    if (label.indexOf("calibration") !== -1) return "sliders";
    if (label.indexOf("trust") !== -1 || label.indexOf("governance") !== -1 || label.indexOf("review") !== -1 || label.indexOf("release") !== -1) return "shield-check";
    if (label.indexOf("assistant") !== -1 || label.indexOf("aria") !== -1) return "bot";
    if (label.indexOf("auth") !== -1 || label.indexOf("security") !== -1) return "key";
    if (label.indexOf("sdk") !== -1 || label.indexOf("plugin") !== -1 || label.indexOf("cli") !== -1) return "code";
    if (label.indexOf("api") !== -1 || label.indexOf("http") !== -1 || label.indexOf("reference") !== -1 || label.indexOf("catalog") !== -1) return "book";
    if (label.indexOf("config") !== -1 || label.indexOf("provider setup") !== -1) return "settings";
    if (label.indexOf("health") !== -1 || label.indexOf("readiness") !== -1 || label.indexOf("observability") !== -1) return "activity";
    if (label.indexOf("recovery") !== -1 || label.indexOf("runbook") !== -1 || label.indexOf("troubleshooting") !== -1) return "lifebuoy";
    if (label.indexOf("architecture") !== -1 || label.indexOf("platform") !== -1 || label.indexOf("refinement") !== -1) return "layers";
    return iconKeyForSection(meta && (meta.section_id || meta.section) || "");
  }

  function makeDocIcon(iconKey, extraClass) {
    var node = document.createElement("span");
    node.className = "docs-icon" + (extraClass ? " " + extraClass : "");
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = iconMarkup(iconKey);
    return node;
  }

  function iconMarkup(iconKey) {
    var body = DOC_ICON_SVGS[iconKey] || DOC_ICON_SVGS.compass;
    return (
      '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" focusable="false" aria-hidden="true">' +
      body +
      "</svg>"
    );
  }

  function mountInlineIcon(node, iconKey) {
    if (!node || !iconKey || node.dataset.iconMounted === "1") return;
    node.innerHTML = iconMarkup(iconKey);
    node.classList.add("has-svg");
    node.dataset.iconMounted = "1";
  }

  /* ---------- Mermaid: capture sources so we can re-theme on toggle ---------- */
  var mermaidNodes = Array.prototype.slice.call(document.querySelectorAll("pre.mermaid"));
  mermaidNodes.forEach(function (n) { n.dataset.src = n.textContent; });

  // Mermaid honours custom themeVariables only under the "base" theme; the
  // built-in "default"/"dark" themes ignore most of them, which is why diagrams
  // used to render in an off-brand lavender. We pin "base" and hand it the
  // CALIBER violet-on-white (light) / violet-on-slate (dark) palette so every
  // diagram matches the rest of the design system.
  var MERMAID_FONT =
    '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif';

  function mermaidConfig() {
    var dark = docRoot.dataset.theme === "dark";
    var vars = dark
      ? {
          background: "transparent",
          primaryColor: "#161f30",
          primaryTextColor: "#e8edf5",
          primaryBorderColor: "#a78bfa",
          secondaryColor: "#131b2a",
          secondaryTextColor: "#cbd5e1",
          secondaryBorderColor: "#324155",
          tertiaryColor: "#0f1726",
          tertiaryTextColor: "#cbd5e1",
          tertiaryBorderColor: "#324155",
          lineColor: "#7c8aa0",
          textColor: "#cbd5e1",
          mainBkg: "#161f30",
          clusterBkg: "rgba(167,139,250,.06)",
          clusterBorder: "#324155",
          edgeLabelBackground: "#0d1320",
          nodeBorder: "#a78bfa",
          // sequence-diagram surfaces
          actorBkg: "#161f30",
          actorBorder: "#a78bfa",
          actorTextColor: "#e8edf5",
          actorLineColor: "#7c8aa0",
          signalColor: "#cbd5e1",
          signalTextColor: "#cbd5e1",
          labelBoxBkgColor: "#161f30",
          labelBoxBorderColor: "#a78bfa",
          labelTextColor: "#e8edf5",
          noteBkgColor: "rgba(167,139,250,.12)",
          noteBorderColor: "#a78bfa",
          noteTextColor: "#e8edf5",
        }
      : {
          background: "transparent",
          primaryColor: "#f4f1fc",
          primaryTextColor: "#27272a",
          primaryBorderColor: "#8f74e0",
          secondaryColor: "#f6f7f9",
          secondaryTextColor: "#3f3f46",
          secondaryBorderColor: "#d8d4ea",
          tertiaryColor: "#fbfaff",
          tertiaryTextColor: "#3f3f46",
          tertiaryBorderColor: "#e7e5ee",
          lineColor: "#9b94ad",
          textColor: "#3f3f46",
          mainBkg: "#f4f1fc",
          clusterBkg: "rgba(143,116,224,.05)",
          clusterBorder: "#e0dbf2",
          edgeLabelBackground: "#ffffff",
          nodeBorder: "#8f74e0",
          // sequence-diagram surfaces
          actorBkg: "#f4f1fc",
          actorBorder: "#8f74e0",
          actorTextColor: "#27272a",
          actorLineColor: "#c7bff0",
          signalColor: "#52525b",
          signalTextColor: "#52525b",
          labelBoxBkgColor: "#f4f1fc",
          labelBoxBorderColor: "#8f74e0",
          labelTextColor: "#27272a",
          noteBkgColor: "#efeafb",
          noteBorderColor: "#8f74e0",
          noteTextColor: "#27272a",
        };
    return {
      startOnLoad: false,
      theme: "base",
      securityLevel: "loose",
      fontFamily: MERMAID_FONT,
      themeVariables: Object.assign({ fontFamily: MERMAID_FONT, fontSize: "14px" }, vars),
      flowchart: { curve: "basis", htmlLabels: true, padding: 14, nodeSpacing: 44, rankSpacing: 60, useMaxWidth: true },
      sequence: { useMaxWidth: true, mirrorActors: false, boxMargin: 8 },
    };
  }

  // Semantic node palette — the typed-color system. Authors tag flowchart nodes
  // with `:::ctrl` / `:::store` / `:::ext` / `:::async` / `:::ui` / `:::user`;
  // we supply the matching classDefs here so the colors are theme-aware and
  // identical on every page. Keep in sync with the legend dots in docs.css.
  function semanticClassDefs(dark) {
    var p = dark
      ? {
          user: "fill:#1a212e,stroke:#94a3b8,color:#cbd5e1",
          ui: "fill:#161f3a,stroke:#818cf8,color:#c7d2fe",
          ctrl: "fill:#241b3a,stroke:#a78bfa,color:#d8ccff",
          store: "fill:#0e2a27,stroke:#2dd4bf,color:#99f6e4",
          ext: "fill:#2e2114,stroke:#f59e0b,color:#fcd9a0",
          async: "fill:#2c1320,stroke:#f472b6,color:#fbcfe1",
        }
      : {
          user: "fill:#f1f3f6,stroke:#8a93a3,color:#3f4654",
          ui: "fill:#e9eefc,stroke:#4f6ef0,color:#243b8a",
          ctrl: "fill:#f0ebff,stroke:#8f74e0,color:#3b2e6b",
          store: "fill:#e6f7f4,stroke:#0e9e8a,color:#0b5a4f",
          ext: "fill:#fdf1e3,stroke:#d98324,color:#7a4a12",
          async: "fill:#fdeaf1,stroke:#d6457f,color:#82264f",
        };
    return Object.keys(p)
      .map(function (k) { return "classDef " + k + " " + p[k] + ",stroke-width:1.5px;"; })
      .join("\n");
  }

  // Inject the classDefs right after the `flowchart`/`graph` declaration so the
  // `:::class` tags resolve. Non-flowchart diagrams (sequence, etc.) pass through.
  function withSemanticClasses(src, dark) {
    var lines = src.replace(/^\s+/, "").split("\n");
    if (!/^(flowchart|graph)\b/.test(lines[0])) return src;
    return lines[0] + "\n" + semanticClassDefs(dark) + "\n" + lines.slice(1).join("\n");
  }

  function renderMermaid() {
    if (!window.mermaid || !mermaidNodes.length) return;
    var dark = docRoot.dataset.theme === "dark";
    mermaidNodes.forEach(function (n) {
      n.removeAttribute("data-processed");
      // Restore via textContent (not innerHTML) so escaped source such as
      // `&lt;br/&gt;` is handed to Mermaid as the literal `<br/>` it expects,
      // instead of being parsed into a stray <br> element and lost.
      n.textContent = withSemanticClasses(n.dataset.src, dark);
    });
    try {
      window.mermaid.initialize(mermaidConfig());
      var ran = window.mermaid.run({ nodes: mermaidNodes });
      // Re-theming on toggle re-creates the <svg>; (re)attach zoom afterwards.
      if (ran && typeof ran.then === "function") {
        ran.then(enhanceAllDiagrams).catch(enhanceAllDiagrams);
      } else {
        enhanceAllDiagrams();
      }
    } catch (e) {
      /* never let a diagram failure break the page */
    }
  }

  /* ---------- Diagram zoom / pan / fullscreen ----------
     Wide diagrams shrink to fit the content column and can get unreadably small,
     so every rendered diagram gets a small toolbar (zoom out / reset / zoom in /
     fullscreen) plus ctrl/⌘-wheel zoom, drag-to-pan, and double-click reset. The
     SVG keeps Mermaid's fit-to-width sizing at scale 1 (whole diagram visible),
     and a CSS transform scales/pans on top within the clipped .diagram box. */
  var ZOOM_MIN = 0.5, ZOOM_MAX = 8;

  function applyZoom(st) {
    if (st.svg) st.svg.style.transform = "translate(" + st.tx + "px," + st.ty + "px) scale(" + st.scale + ")";
  }
  function resetZoom(st) { st.scale = 1; st.tx = 0; st.ty = 0; applyZoom(st); }
  function zoomAround(st, factor, clientX, clientY) {
    var next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, st.scale * factor));
    var rect = st.diagram.getBoundingClientRect();
    var ox = clientX == null ? rect.width / 2 : clientX - rect.left;
    var oy = clientY == null ? rect.height / 2 : clientY - rect.top;
    // Keep the point under the cursor fixed while scaling.
    st.tx = ox - (ox - st.tx) * (next / st.scale);
    st.ty = oy - (oy - st.ty) * (next / st.scale);
    st.scale = next;
    applyZoom(st);
  }

  function enhanceAllDiagrams() {
    var diagrams = document.querySelectorAll(".diagram");
    Array.prototype.forEach.call(diagrams, enhanceDiagram);
  }

  function enhanceDiagram(diagram) {
    var svg = diagram.querySelector("svg");
    if (!svg) return;
    svg.style.transformOrigin = "0 0";
    svg.style.cursor = "grab";

    // Re-render (theme toggle): rebind the fresh <svg> and reset, keep toolbar.
    if (diagram.__zoom) {
      diagram.__zoom.svg = svg;
      resetZoom(diagram.__zoom);
      return;
    }

    var st = (diagram.__zoom = { diagram: diagram, svg: svg, scale: 1, tx: 0, ty: 0 });
    applyZoom(st);

    var bar = document.createElement("div");
    bar.className = "diagram-zoom";
    bar.innerHTML =
      '<button type="button" data-z="out" aria-label="Zoom out" title="Zoom out">−</button>' +
      '<button type="button" data-z="reset" aria-label="Reset zoom" title="Reset zoom">⟲</button>' +
      '<button type="button" data-z="in" aria-label="Zoom in" title="Zoom in">+</button>' +
      '<button type="button" data-z="full" aria-label="Fullscreen" title="Fullscreen (Esc to close)">⛶</button>';
    diagram.appendChild(bar);

    bar.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("button") : null;
      if (!btn) return;
      var z = btn.getAttribute("data-z");
      if (z === "in") zoomAround(st, 1.3, null, null);
      else if (z === "out") zoomAround(st, 1 / 1.3, null, null);
      else if (z === "reset") resetZoom(st);
      else if (z === "full") toggleFull(diagram, st);
    });

    // ctrl/⌘ + wheel to zoom (plain scroll still scrolls the page).
    diagram.addEventListener(
      "wheel",
      function (e) {
        if (!e.ctrlKey && !e.metaKey && !diagram.classList.contains("is-full")) return;
        e.preventDefault();
        zoomAround(st, e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
      },
      { passive: false }
    );

    // Drag to pan.
    var dragging = false, sx = 0, sy = 0;
    diagram.addEventListener("pointerdown", function (e) {
      if (e.target.closest && e.target.closest(".diagram-zoom")) return;
      dragging = true;
      sx = e.clientX - st.tx;
      sy = e.clientY - st.ty;
      svg.style.cursor = "grabbing";
      try { diagram.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
    });
    diagram.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      st.tx = e.clientX - sx;
      st.ty = e.clientY - sy;
      applyZoom(st);
    });
    function endDrag() { dragging = false; if (st.svg) st.svg.style.cursor = "grab"; }
    diagram.addEventListener("pointerup", endDrag);
    diagram.addEventListener("pointercancel", endDrag);
    diagram.addEventListener("dblclick", function () { resetZoom(st); });
  }

  function toggleFull(diagram, st) {
    var full = diagram.classList.toggle("is-full");
    document.body.classList.toggle("diagram-full-open", full);
    var btn = diagram.querySelector('.diagram-zoom [data-z="full"]');
    if (btn) {
      btn.textContent = full ? "✕" : "⛶";
      btn.setAttribute("title", full ? "Close (Esc)" : "Fullscreen (Esc to close)");
      btn.setAttribute("aria-label", full ? "Close fullscreen" : "Fullscreen");
    }
    // In fullscreen the wide container lets Mermaid's fit-to-width render the
    // diagram large; reset so it starts centered and readable.
    resetZoom(st);
  }

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var open = document.querySelector(".diagram.is-full");
    if (open && open.__zoom) toggleFull(open, open.__zoom);
  });

  // Mermaid sizes node boxes by measuring label width. If it measures before the
  // Inter web font has loaded, it uses the (narrower) fallback metrics and the
  // real glyphs overflow — labels get clipped on the right. Wait for the font to
  // be ready so the measurement matches what actually paints.
  if (document.fonts && document.fonts.ready && typeof document.fonts.ready.then === "function") {
    document.fonts.ready.then(renderMermaid);
  } else {
    renderMermaid();
  }

  /* ---------- Theme toggle ---------- */
  var themeToggle = document.getElementById("themeToggle");
  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var next = docRoot.dataset.theme === "dark" ? "light" : "dark";
      docRoot.dataset.theme = next;
      try { localStorage.setItem("caliber-docs-theme", next); } catch (e) {}
      renderMermaid();
    });
  }

  /* ---------- Copy page action ---------- */
  var copyPageButton = document.querySelector("[data-copy-page]");
  var copyPageTimer = 0;

  function updateCopyButton(label, copied) {
    if (!copyPageButton) return;
    copyPageButton.textContent = label;
    copyPageButton.classList.toggle("is-copied", Boolean(copied));
  }

  function legacyCopyText(text) {
    return new Promise(function (resolve, reject) {
      var input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.top = "-9999px";
      document.body.appendChild(input);
      input.select();
      try {
        if (!document.execCommand("copy")) throw new Error("copy command failed");
        document.body.removeChild(input);
        resolve();
      } catch (err) {
        document.body.removeChild(input);
        reject(err);
      }
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () {
        return legacyCopyText(text);
      });
    }
    return legacyCopyText(text);
  }

  if (copyPageButton) {
    copyPageButton.addEventListener("click", function () {
      var defaultLabel = copyPageButton.getAttribute("data-copy-default") || "Copy page";
      var successLabel = copyPageButton.getAttribute("data-copy-success") || "Copied";
      var failureLabel = copyPageButton.getAttribute("data-copy-failure") || "Copy failed";
      window.clearTimeout(copyPageTimer);
      copyText(location.href)
        .then(function () {
          updateCopyButton(successLabel, true);
          copyPageTimer = window.setTimeout(function () {
            updateCopyButton(defaultLabel, false);
          }, 1400);
        })
        .catch(function () {
          updateCopyButton(failureLabel, false);
          copyPageTimer = window.setTimeout(function () {
            updateCopyButton(defaultLabel, false);
          }, 1600);
        });
    });
  }

  /* ---------- Sidebar built from window.DOCS_NAV ---------- */
  var navRoot = document.getElementById("docs-nav");
  var filterInput = document.getElementById("nav-filter");
  var landingSearch = document.getElementById("landingSearch");
  var landingSearchEmpty = document.getElementById("landingSearchEmpty");
  var searchTrigger = document.getElementById("topbarSearch");
  var sectionTabsRoot = document.getElementById("docsSectionTabs");
  var sidebar = document.getElementById("docsSidebar") || document.querySelector(".sidebar");
  var menuToggle = document.getElementById("menuToggle");
  var searchKeycaps = document.querySelectorAll("[data-doc-search-key]");
  var topbarBrowseMenu = null;
  var docsData = window.DOCS_DATA || { sections: window.DOCS_NAV || [], pages: [] };
  var docsSections = Array.isArray(docsData.sections) ? docsData.sections : (Array.isArray(window.DOCS_NAV) ? window.DOCS_NAV : []);
  var docsPages = Array.isArray(docsData.pages) ? docsData.pages : [];
  var currentPage = (location.pathname.split("/").pop() || "index.html").toLowerCase();
  if (!currentPage || currentPage === "") currentPage = "index.html";
  var pageMetaByPage = {};
  docsPages.forEach(function (page) {
    if (!page || !page.href) return;
    pageMetaByPage[pageOf(page.href)] = page;
  });

  function pageOf(href) {
    var p = (href || "").split("#")[0].split("/").pop().toLowerCase();
    return p || "index.html";
  }

  function pageMetaForHref(href, fallback) {
    return pageMetaByPage[pageOf(href)] || fallback || null;
  }

  function wrapLabelWithIcon(el, iconKey, iconClass) {
    if (!el || !iconKey || el.dataset.iconized === "1") return;
    var text = el.textContent;
    el.textContent = "";
    el.appendChild(makeDocIcon(iconKey, iconClass || "docs-icon-sm docs-icon-muted"));
    var label = document.createElement("span");
    label.textContent = text;
    el.appendChild(label);
    el.dataset.iconized = "1";
  }

  function decorateCardLink(link, meta) {
    if (!link || link.dataset.iconized === "1") return;
    var iconKey = iconKeyForPage(meta || pageMetaForHref(link.getAttribute("href"), {
      href: link.getAttribute("href") || "",
      label: (link.querySelector("strong") || link).textContent,
      section: ""
    }));
    link.insertBefore(makeDocIcon(iconKey, "docs-icon-sm"), link.firstChild);
    link.dataset.iconized = "1";
  }

  function humanizeToken(value) {
    return String(value || "")
      .split(/[-_]/)
      .filter(Boolean)
      .map(function (part) {
        return part.charAt(0).toUpperCase() + part.slice(1);
      })
      .join(" ");
  }

  function formatAudienceLabel(audience) {
    if (audience === "system-user") return "System user";
    if (audience === "decision-maker") return "Decision-maker";
    return humanizeToken(audience);
  }

  function formatDocType(docType) {
    return humanizeToken(docType);
  }

  function buildDocMetaElement(meta, options) {
    var pageMeta = meta || {};
    var opts = options || {};
    var audience = Array.isArray(pageMeta.audience) ? pageMeta.audience : [];
    var prerequisites = Array.isArray(pageMeta.prerequisites) ? pageMeta.prerequisites : [];
    var hasBasics = audience.length || pageMeta.doc_type || pageMeta.stability;
    var hasSummary = opts.showSummary && pageMeta.summary;
    var hasNotes = pageMeta.reviewed_on || pageMeta.version_applicability;
    if (!hasBasics && !hasSummary && !prerequisites.length && !hasNotes) return null;

    var root = document.createElement("div");
    root.className = "doc-meta";

    if (hasBasics) {
      var chips = document.createElement("div");
      chips.className = "doc-meta-chips";
      audience.forEach(function (item) {
        var chip = document.createElement("span");
        chip.className = "doc-meta-chip";
        chip.textContent = formatAudienceLabel(item);
        chips.appendChild(chip);
      });
      if (pageMeta.doc_type) {
        var typeChip = document.createElement("span");
        typeChip.className = "doc-meta-chip doc-meta-chip-type";
        typeChip.textContent = formatDocType(pageMeta.doc_type);
        chips.appendChild(typeChip);
      }
      if (pageMeta.stability) {
        var stabilityChip = document.createElement("span");
        stabilityChip.className = "doc-meta-chip doc-meta-chip-stability";
        stabilityChip.textContent = String(pageMeta.stability).toUpperCase();
        chips.appendChild(stabilityChip);
      }
      root.appendChild(chips);
    }

    if (hasSummary) {
      var summary = document.createElement("div");
      summary.className = "doc-summary";
      summary.textContent = pageMeta.summary;
      root.appendChild(summary);
    }

    if (prerequisites.length) {
      var extra = document.createElement("div");
      extra.className = "doc-meta-extra";
      var label = document.createElement("span");
      label.className = "doc-meta-label";
      label.textContent = "Prerequisites";
      extra.appendChild(label);
      var values = document.createElement("span");
      values.className = "doc-meta-prereqs";
      values.textContent = prerequisites.join(" · ");
      extra.appendChild(values);
      root.appendChild(extra);
    }

    if (hasNotes) {
      var notes = [];
      if (pageMeta.reviewed_on) notes.push("Reviewed " + pageMeta.reviewed_on);
      if (pageMeta.version_applicability) notes.push(pageMeta.version_applicability);
      var noteEl = document.createElement("div");
      noteEl.className = "doc-meta-notes";
      noteEl.textContent = notes.join(" · ");
      root.appendChild(noteEl);
    }

    return root;
  }

  function injectHandAuthoredPageMeta() {
    if (!document.body || document.body.dataset.docPage !== "module") return;
    var article = document.querySelector("article.content");
    if (!article) return;
    if (article.querySelector(".doc-header .doc-meta")) return;
    if (article.querySelector(".doc-breadcrumb + .doc-meta")) return;
    var meta = pageMetaByPage[currentPage];
    if (!meta) return;
    var metaEl = buildDocMetaElement(meta, { showSummary: true });
    if (!metaEl) return;
    var breadcrumb = article.querySelector(".doc-breadcrumb");
    if (breadcrumb && breadcrumb.parentNode === article) {
      article.insertBefore(metaEl, breadcrumb.nextSibling);
      return;
    }
    article.insertBefore(metaEl, article.firstChild);
  }

  function setSidebarOpen(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", Boolean(open));
    if (menuToggle) menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function sectionLabel(sec) {
    return (sec && sec.section) || "";
  }

  function sectionHref(sec) {
    return sec && sec.links && sec.links[0] && sec.links[0].href ? sec.links[0].href : "index.html";
  }

  function sectionIsCurrent(sec) {
    return Boolean((sec && sec.links || []).some(function (lnk) {
      return pageOf(lnk.href) === currentPage;
    }));
  }

  function currentSection() {
    for (var i = 0; i < docsSections.length; i++) {
      if (sectionIsCurrent(docsSections[i])) return docsSections[i];
    }
    return null;
  }

  function sectionSummary(sec) {
    if (sec && sec.summary) return sec.summary;
    switch (sectionLabel(sec)) {
      case "Start here":
        return "Product overview, quickstart, and documentation paths.";
      case "Use CALIBER":
        return "Feature areas and concepts for using the product.";
      case "Build & integrate":
        return "SDK, API, authentication, and automation guidance.";
      case "Operate CALIBER":
        return "Bring-up, observability, recovery, and operator guidance.";
      case "Examples":
        return "Cookbooks, recipes, and runnable end-to-end implementation patterns.";
      case "Reference":
        return "Exact API, SDK, and component lookup material.";
      case "Architecture":
        return "Deep platform architecture, trust boundaries, and implementation concepts.";
      case "Strategy":
        return "Roadmap, market context, and supporting collateral.";
      default:
        return "Browse this documentation section.";
    }
  }

  searchKeycaps.forEach(function (el) {
    var platform = navigator.platform || navigator.userAgent || "";
    el.textContent = /Mac|iPhone|iPad/i.test(platform) ? "\u2318K" : "Ctrl K";
  });

  if (menuToggle) {
    menuToggle.addEventListener("click", function () {
      setSidebarOpen(!(sidebar && sidebar.classList.contains("open")));
    });
  }

  if (navRoot && docsSections.length) {
    var activeSectionForNav = document.body && document.body.dataset.docPage === "module"
      ? currentSection()
      : null;
    var navSections = docsSections.slice();
    if (activeSectionForNav) {
      navSections.sort(function (a, b) {
        if (a === activeSectionForNav) return -1;
        if (b === activeSectionForNav) return 1;
        return 0;
      });
    }
    var frag = document.createDocumentFragment();
    navSections.forEach(function (sec, idx) {
      var head = document.createElement("div");
      head.className = "nav-section";
      head.appendChild(makeDocIcon(iconKeyForSection(sec.section), "docs-icon-sm docs-icon-muted"));
      head.appendChild(document.createTextNode(sec.section));
      frag.appendChild(head);
      (sec.links || []).forEach(function (lnk) {
        var meta = pageMetaForHref(lnk.href, { href: lnk.href, label: lnk.label, section: sec.section });
        var a = document.createElement("a");
        a.className = "nav-link";
        a.href = lnk.href;
        a.appendChild(makeDocIcon(iconKeyForPage(meta), "docs-icon-sm docs-icon-muted"));
        var label = document.createElement("span");
        label.className = "nav-link-label";
        label.textContent = lnk.label;
        // Standalone full-screen views (e.g. the slide deck) open in a new tab so
        // the reader never loses the docs shell. Flagged via DOCS_NAV.
        if (lnk.newtab) {
          a.target = "_blank";
          a.rel = "noopener";
          a.classList.add("nav-link-external");
          var mark = document.createElement("span");
          mark.className = "nav-link-external-mark";
          mark.setAttribute("aria-hidden", "true");
          mark.textContent = "↗";
          label.appendChild(mark);
        } else if (pageOf(lnk.href) === currentPage) {
          a.classList.add("active");
        }
        a.appendChild(label);
        frag.appendChild(a);
      });
      if (idx === 0 && activeSectionForNav && navSections.length > 1) {
        var divider = document.createElement("div");
        divider.className = "nav-section nav-section-secondary";
        divider.textContent = "All docs";
        frag.appendChild(divider);
      }
    });
    navRoot.appendChild(frag);
  }

  if (sectionTabsRoot && docsSections.length) {
    var currentSectionRef = null;
    docsSections.forEach(function (sec) {
      if (!currentSectionRef && sectionIsCurrent(sec)) currentSectionRef = sec;
    });
    if (!currentSectionRef) currentSectionRef = docsSections[0] || null;

    var topbarFrag = document.createDocumentFragment();
    topbarBrowseMenu = document.createElement("details");
    topbarBrowseMenu.className = "docs-browse-menu";

    var browseSummary = document.createElement("summary");
    browseSummary.className = "docs-browse-trigger";
    browseSummary.setAttribute("aria-label", "Browse docs");
    browseSummary.innerHTML =
      '<span class="docs-browse-trigger-label">Browse docs</span>' +
      '<svg class="docs-browse-trigger-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg>';
    topbarBrowseMenu.appendChild(browseSummary);

    var browsePanel = document.createElement("div");
    browsePanel.className = "docs-browse-panel";

    var browseList = document.createElement("div");
    browseList.className = "docs-browse-list";
    docsSections.forEach(function (sec) {
      if (!sec || !sec.links || !sec.links.length) return;
      var item = document.createElement("a");
      item.className = "docs-browse-item";
      item.href = sectionHref(sec);
      if (sectionIsCurrent(sec)) item.classList.add("active");

      var title = document.createElement("span");
      title.className = "docs-browse-item-title";
      title.appendChild(makeDocIcon(iconKeyForSection(sec.section), "docs-icon-sm"));
      var titleText = document.createElement("span");
      titleText.textContent = sectionLabel(sec);
      title.appendChild(titleText);

      var summary = document.createElement("span");
      summary.className = "docs-browse-item-summary";
      summary.textContent = sectionSummary(sec);

      var meta = document.createElement("span");
      meta.className = "docs-browse-item-meta";
      meta.textContent = (sec.links || []).length === 1 ? "1 page" : (sec.links || []).length + " pages";

      item.appendChild(title);
      item.appendChild(summary);
      item.appendChild(meta);
      browseList.appendChild(item);
    });
    browsePanel.appendChild(browseList);
    topbarBrowseMenu.appendChild(browsePanel);
    topbarFrag.appendChild(topbarBrowseMenu);

    if (currentSectionRef) {
      var activeSection = document.createElement("a");
      activeSection.className = "topbar-pill topbar-active-section";
      activeSection.href = sectionHref(currentSectionRef);
      activeSection.appendChild(makeDocIcon(iconKeyForSection(currentSectionRef.section), "docs-icon-sm"));
      activeSection.appendChild(document.createTextNode(sectionLabel(currentSectionRef)));
      activeSection.setAttribute("aria-label", "Current section: " + sectionLabel(currentSectionRef));
      topbarFrag.appendChild(activeSection);
    }

    sectionTabsRoot.appendChild(topbarFrag);
  }

  function mountStaticDocIcons() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-doc-icon]"), function (node) {
      mountInlineIcon(node, node.getAttribute("data-doc-icon"));
    });
  }

  function enhanceDocChrome() {
    var meta = pageMetaForHref(currentPage, {
      href: currentPage,
      label: (document.querySelector(".doc-breadcrumb .current") || {}).textContent || currentPage,
      section_id: "",
      section: ""
    });
    var pageIcon = iconKeyForPage(meta);
    var sectionIcon = iconKeyForSection(meta && (meta.section_id || meta.section) || "");

    var eyebrow = document.querySelector(".doc-eyebrow");
    if (eyebrow && !eyebrow.querySelector("svg")) {
      eyebrow.insertBefore(makeDocIcon(pageIcon, "docs-icon-sm"), eyebrow.firstChild);
    }

    var breadcrumb = document.querySelector(".doc-breadcrumb");
    if (breadcrumb) {
      var breadcrumbItems = breadcrumb.querySelectorAll("a, span");
      Array.prototype.forEach.call(breadcrumbItems, function (item) {
        if (item.getAttribute("aria-hidden") === "true") return;
        if (item.classList.contains("current")) wrapLabelWithIcon(item, pageIcon, "docs-icon-sm docs-icon-muted");
        else if (!item.querySelector(".docs-icon")) wrapLabelWithIcon(item, sectionIcon, "docs-icon-sm docs-icon-muted");
        item.classList.add("docs-crumb");
      });
    }
  }

  function enhanceLandingCards() {
    Array.prototype.forEach.call(document.querySelectorAll("a.quickstart-card, a.guide-map-card, a.hero-panel-link"), function (link) {
      decorateCardLink(link);
    });
  }

  function focusDocSearch() {
    if (landingSearch) {
      var panel = landingSearch.closest(".landing-search-panel");
      if (panel && typeof panel.scrollIntoView === "function") {
        panel.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      landingSearch.focus();
      landingSearch.select();
      return;
    }
    if (!filterInput) return;
    setSidebarOpen(true);
    filterInput.focus();
    filterInput.select();
  }

  if (searchTrigger) {
    searchTrigger.addEventListener("click", function () {
      focusDocSearch();
    });
  }

  document.addEventListener("keydown", function (e) {
    var key = String(e.key || "");
    if (key === "Escape" && topbarBrowseMenu && topbarBrowseMenu.open) {
      topbarBrowseMenu.open = false;
      var browseTrigger = topbarBrowseMenu.querySelector("summary");
      if (browseTrigger && typeof browseTrigger.focus === "function") browseTrigger.focus();
      return;
    }
    if (key === "Escape" && sidebar && sidebar.classList.contains("open")) {
      setSidebarOpen(false);
      if (filterInput === document.activeElement) filterInput.blur();
      return;
    }
    if (isEditableTarget(document.activeElement)) return;
    if ((e.metaKey || e.ctrlKey) && key.toLowerCase() === "k") {
      if (!filterInput) return;
      e.preventDefault();
      focusDocSearch();
      return;
    }
    if (!e.metaKey && !e.ctrlKey && !e.altKey && key === "/") {
      if (!filterInput) return;
      e.preventDefault();
      focusDocSearch();
      return;
    }
  });

  document.addEventListener("click", function (e) {
    if (!topbarBrowseMenu || !topbarBrowseMenu.open) return;
    if (topbarBrowseMenu.contains(e.target)) return;
    topbarBrowseMenu.open = false;
  });

  /* ---------- Page-to-page navigation (Prev / Next) ---------- */
  function flattenPages(nav) {
    var pages = [];
    (nav || []).forEach(function (sec) {
      (sec.links || []).forEach(function (lnk) {
        if (!lnk || !lnk.href) return;
        var page = pageOf(lnk.href);
        if (pages.some(function (p) { return pageOf(p.href) === page; })) return;
        pages.push({
          href: String(lnk.href).split("#")[0],
          label: lnk.label || page,
          section: sec.section || "",
        });
      });
    });
    return pages;
  }

  function isEditableTarget(el) {
    if (!el) return false;
    var tag = (el.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    return Boolean(el.isContentEditable);
  }

  function pagerSubLabel(page) {
    if (!page) return "";
    var sec = String(page.section || "").trim();
    var label = String(page.label || "").trim();
    if (!sec) return "";
    if (!label) return sec;
    return sec.toLowerCase() === label.toLowerCase() ? "" : sec;
  }

  function buildPagerLink(dir, page) {
    if (!page) {
      var empty = document.createElement("div");
      empty.className = "doc-pager-spacer";
      return empty;
    }
    var a = document.createElement("a");
    a.className = "doc-pager-link doc-pager-" + dir;
    a.href = page.href;
    a.setAttribute("rel", dir === "prev" ? "prev" : "next");

    var kick = document.createElement("div");
    kick.className = "doc-pager-kicker";
    kick.textContent = dir === "prev" ? "Previous" : "Next";
    a.appendChild(kick);

    var title = document.createElement("div");
    title.className = "doc-pager-title";
    title.textContent = String(page.label || pageOf(page.href));
    a.appendChild(title);

    var sub = pagerSubLabel(page);
    if (sub) {
      var hint = document.createElement("div");
      hint.className = "doc-pager-hint";
      hint.textContent = sub;
      a.appendChild(hint);
    }

    return a;
  }

  function renderPager() {
    if (!docsSections.length) return;
    if (document.body && document.body.dataset.docPage === "landing") return;
    var currentMeta = pageMetaByPage[currentPage] || null;
    if (!currentMeta || !currentMeta.section_id) return;
    var pages = docsPages
      .filter(function (page) {
        return page &&
          page.section_id === currentMeta.section_id &&
          !page.nav_hidden &&
          page.href &&
          pageOf(page.href) !== "index.html";
      })
      .slice()
      .sort(function (a, b) {
        var orderA = typeof a.nav_order === "number" ? a.nav_order : 9999;
        var orderB = typeof b.nav_order === "number" ? b.nav_order : 9999;
        if (orderA !== orderB) return orderA - orderB;
        return String(a.label || "").localeCompare(String(b.label || ""));
      })
      .map(function (page) {
        return {
          href: String(page.href).split("#")[0],
          label: page.label || pageOf(page.href),
          section: page.section || "",
        };
      });
    if (!pages.length) return;

    var idx = -1;
    pages.forEach(function (p, i) {
      if (pageOf(p.href) === currentPage) idx = i;
    });
    if (idx < 0) return;

    var prev = idx > 0 ? pages[idx - 1] : null;
    var next = idx < pages.length - 1 ? pages[idx + 1] : null;
    if (!prev && !next) return;

    var article = document.querySelector("article.content");
    if (!article) return;

    var nav = document.createElement("nav");
    nav.className = "doc-pager";
    nav.setAttribute("aria-label", "Page navigation");

    nav.appendChild(buildPagerLink("prev", prev));

    var meta = document.createElement("div");
    meta.className = "doc-pager-meta";
    meta.textContent = (currentMeta.section || "Section") + " \u00b7 " + String(idx + 1) + " of " + String(pages.length) + " \u00b7 Alt+\u2190 / Alt+\u2192";
    nav.appendChild(meta);

    nav.appendChild(buildPagerLink("next", next));

    var footer = article.querySelector("footer");
    if (footer) {
      article.insertBefore(nav, footer);
    } else {
      article.appendChild(nav);
    }

    document.addEventListener("keydown", function (e) {
      if (isEditableTarget(document.activeElement)) return;
      var key = e.key;
      if ((e.altKey && key === "ArrowLeft") || (!e.altKey && !e.metaKey && !e.ctrlKey && key === "[")) {
        if (prev) {
          e.preventDefault();
          location.href = prev.href;
        }
      }
      if ((e.altKey && key === "ArrowRight") || (!e.altKey && !e.metaKey && !e.ctrlKey && key === "]")) {
        if (next) {
          e.preventDefault();
          location.href = next.href;
        }
      }
    });
  }
  renderPager();
  injectHandAuthoredPageMeta();
  mountStaticDocIcons();
  enhanceDocChrome();
  enhanceLandingCards();

  /* ---------- Per-page table of contents ---------- */
  // Module pages tag their <h2> with ids; the landing page uses <section id>.
  var content = document.querySelector(".content");
  var tocTargets = [];
  if (content) {
    var headings = content.querySelectorAll("h2[id], h3[id]");
    if (headings.length) {
      tocTargets = Array.prototype.slice.call(headings).map(function (h) {
        return {
          id: h.id,
          text: h.textContent.replace(/#\s*$/, "").trim(),
          el: h,
          level: Number((h.tagName || "H2").slice(1)),
        };
      });
      if (tocTargets.length > 24 && tocTargets.some(function (t) { return t.level === 3; })) {
        tocTargets = tocTargets.filter(function (t) { return t.level === 2; });
      }
    } else {
      var secs = content.querySelectorAll("section[id]");
      tocTargets = Array.prototype.slice
        .call(secs)
        .filter(function (s) { return s.querySelector("h2"); })
        .map(function (s) {
          return { id: s.id, text: s.querySelector("h2").textContent.trim(), el: s, level: 2 };
        });
    }
  }

  var tocRoot = document.getElementById("page-toc");
  var tocById = {};
  if (tocRoot && tocTargets.length) {
    tocTargets.forEach(function (t) {
      var a = document.createElement("a");
      a.href = "#" + t.id;
      a.textContent = t.text;
      if (t.level > 2) a.classList.add("toc-link-level-" + String(t.level));
      tocRoot.appendChild(a);
      tocById[t.id] = a;
    });
  } else if (tocRoot) {
    // No headings to index — hide the empty TOC card.
    var card = tocRoot.closest(".toc");
    if (card) card.style.display = "none";
  }

  /* ---------- Scroll-spy for the TOC ---------- */
  if (tocTargets.length && Object.keys(tocById).length && "IntersectionObserver" in window) {
    var observer = new IntersectionObserver(
      function (entries) {
        var visible = entries
          .filter(function (e) { return e.isIntersecting; })
          .sort(function (a, b) { return a.boundingClientRect.top - b.boundingClientRect.top; });
        if (visible.length) {
          Object.keys(tocById).forEach(function (id) { tocById[id].classList.remove("active"); });
          var t = tocById[visible[0].target.id];
          if (t) t.classList.add("active");
        }
      },
      { rootMargin: "-20% 0px -70% 0px", threshold: 0 }
    );
    tocTargets.forEach(function (t) { observer.observe(t.el); });
  }

  /* ---------- Reveal collapsed Reference panels on anchor navigation ---------- */
  // A heading inside a default-open <details.ref-section> is still reachable, but
  // if a reader collapses one and then follows a TOC link or #hash to a heading
  // inside it, open the panel first so the target actually scrolls into view.
  function revealHash(hash) {
    if (!hash || hash.length < 2) return;
    var target;
    try { target = document.getElementById(decodeURIComponent(hash.slice(1))); }
    catch (e) { target = document.getElementById(hash.slice(1)); }
    if (!target) return;
    var d = target.closest ? target.closest("details.ref-section") : null;
    if (d && !d.open) {
      d.open = true;
      requestAnimationFrame(function () { target.scrollIntoView({ block: "start" }); });
    }
  }
  window.addEventListener("hashchange", function () { revealHash(location.hash); });
  if (content) {
    content.addEventListener("click", function (e) {
      var a = e.target.closest ? e.target.closest('a[href^="#"]') : null;
      if (a) revealHash(a.getAttribute("href"));
    });
  }
  if (location.hash) revealHash(location.hash);

  /* ---------- Search index ---------- */
  var searchIndexPromise = null;

  function searchIndexPath() {
    return "search-index.json";
  }

  function loadSearchIndex() {
    if (searchIndexPromise) return searchIndexPromise;
    searchIndexPromise = fetch(searchIndexPath(), { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("search index unavailable");
        return resp.json();
      })
      .then(function (payload) {
        return Array.isArray(payload && payload.pages) ? payload.pages : [];
      })
      .catch(function () {
        return [];
      });
    return searchIndexPromise;
  }

  function searchTerms(query) {
    return String(query || "")
      .toLowerCase()
      .split(/\s+/)
      .map(function (term) { return term.trim(); })
      .filter(Boolean);
  }

  function scoreSearchEntry(entry, query, terms) {
    if (!entry) return 0;
    var title = String(entry.label || "").toLowerCase();
    var pageLabel = String(entry.page_label || "").toLowerCase();
    var href = String(entry.href || "").toLowerCase();
    var section = String(entry.section || "").toLowerCase();
    var summary = String(entry.summary || "").toLowerCase();
    var symbolList = Array.isArray(entry.symbols) ? entry.symbols.map(function (item) { return String(item || ""); }) : [];
    var routeList = Array.isArray(entry.routes) ? entry.routes.map(function (item) { return String(item || ""); }) : [];
    var headingList = Array.isArray(entry.headings) ? entry.headings.map(function (item) { return String(item || ""); }) : [];
    var symbols = symbolList.join(" ").toLowerCase();
    var routes = routeList.join(" ").toLowerCase();
    var headings = headingList.join(" ").toLowerCase();
    var body = String(entry.body || "").toLowerCase();
    var tags = Array.isArray(entry.tags) ? entry.tags.join(" ").toLowerCase() : "";
    var docType = String(entry.doc_type || "").toLowerCase();
    var audience = Array.isArray(entry.audience) ? entry.audience.join(" ").toLowerCase() : "";
    var isReferencePage = docType === "reference" || section === "reference";
    var full = [title, pageLabel, href, section, summary, symbols, routes, headings, body, tags, docType, audience].join(" ");
    var fullQuery = String(query || "").toLowerCase();
    var exactSymbolMatch = symbolList.some(function (item) { return item.toLowerCase() === fullQuery; });
    var exactRouteMatch = routeList.some(function (item) { return item.toLowerCase() === fullQuery; });
    var exactHeadingMatch = headingList.some(function (item) { return item.toLowerCase() === fullQuery; });
    var score = 0;
    if (title === fullQuery) score += 140;
    if (pageLabel === fullQuery) score += 95;
    if (exactSymbolMatch) score += 220;
    if (exactRouteMatch) score += 210;
    if (exactHeadingMatch) score += 120;
    if (symbols.indexOf(fullQuery) !== -1) score += 135;
    if (routes.indexOf(fullQuery) !== -1) score += 125;
    if (isReferencePage && symbols.indexOf(fullQuery) !== -1) score += 80;
    if (isReferencePage && routes.indexOf(fullQuery) !== -1) score += 80;
    if (href.indexOf(fullQuery) !== -1) score += 110;
    if (title.indexOf(fullQuery) !== -1) score += 100;
    if (pageLabel.indexOf(fullQuery) !== -1) score += 65;
    if (symbols.indexOf(fullQuery) !== -1) score += 50;
    if (routes.indexOf(fullQuery) !== -1) score += 45;
    if (headings.indexOf(fullQuery) !== -1) score += 70;
    if (summary.indexOf(fullQuery) !== -1) score += 55;
    if (section.indexOf(fullQuery) !== -1) score += 35;
    if (docType.indexOf(fullQuery) !== -1) score += 20;
    if (!score && !terms.length) return 0;
    for (var i = 0; i < terms.length; i++) {
      var term = terms[i];
      if (!term) continue;
      if (title.indexOf(term) !== -1) score += 18;
      if (pageLabel.indexOf(term) !== -1) score += 10;
      if (symbols.indexOf(term) !== -1) score += 26;
      if (routes.indexOf(term) !== -1) score += 24;
      if (headings.indexOf(term) !== -1) score += 12;
      if (summary.indexOf(term) !== -1) score += 8;
      if (tags.indexOf(term) !== -1) score += 6;
      if (body.indexOf(term) !== -1) score += 3;
      if (full.indexOf(term) === -1) return 0;
    }
    return score;
  }

  function runSearch(index, query, limit) {
    var terms = searchTerms(query);
    var fullQuery = String(query || "").trim().toLowerCase();
    if (!fullQuery) return [];
    return (index || [])
      .map(function (entry) {
        return { entry: entry, score: scoreSearchEntry(entry, fullQuery, terms) };
      })
      .filter(function (item) { return item.score > 0; })
      .sort(function (a, b) {
        if (b.score !== a.score) return b.score - a.score;
        if ((a.entry.result_type || "page") !== (b.entry.result_type || "page")) {
          return (a.entry.result_type || "page") === "page" ? -1 : 1;
        }
        return String(a.entry.label || "").localeCompare(String(b.entry.label || ""));
      })
      .slice(0, limit || 12)
      .map(function (item) { return item.entry; });
  }

  function resultMeta(entry) {
    var bits = [];
    if (entry.section) bits.push(entry.section);
    if (entry.page_label) bits.push(entry.page_label);
    if ((entry.result_type || "page") === "anchor") bits.push("In-page match");
    if (entry.doc_type) bits.push(String(entry.doc_type).replace(/-/g, " "));
    return bits.join(" · ");
  }

  function groupSearchResults(results) {
    var groups = [];
    (results || []).forEach(function (entry) {
      var section = entry && entry.section ? entry.section : "Documentation";
      var existing = null;
      for (var i = 0; i < groups.length; i++) {
        if (groups[i].section === section) {
          existing = groups[i];
          break;
        }
      }
      if (!existing) {
        existing = { section: section, entries: [] };
        groups.push(existing);
      }
      existing.entries.push(entry);
    });
    return groups;
  }

  function renderNavSearchResults(results, query) {
    if (!navRoot) return;
    navRoot.innerHTML = "";
    var head = document.createElement("div");
    head.className = "nav-section";
    head.textContent = results.length ? 'Search results' : 'No results';
    navRoot.appendChild(head);
    if (!results.length) {
      var empty = document.createElement("div");
      empty.className = "nav-search-empty";
      empty.textContent = 'No documentation pages matched "' + query + '".';
      navRoot.appendChild(empty);
      return;
    }
    groupSearchResults(results).forEach(function (group, index) {
      if (index > 0) {
        var sectionHead = document.createElement("div");
        sectionHead.className = "nav-section nav-search-group";
        sectionHead.appendChild(makeDocIcon(iconKeyForSection(group.section), "docs-icon-sm docs-icon-muted"));
        sectionHead.appendChild(document.createTextNode(group.section));
        navRoot.appendChild(sectionHead);
      }
      group.entries.forEach(function (entry) {
        var a = document.createElement("a");
        a.className = "nav-link nav-search-link";
        a.href = entry.href;
        var title = document.createElement("span");
        title.className = "nav-search-title";
        title.appendChild(makeDocIcon(iconKeyForPage(entry), "docs-icon-sm docs-icon-muted"));
        var titleText = document.createElement("span");
        titleText.textContent = entry.label || entry.href;
        title.appendChild(titleText);
        a.appendChild(title);
        var meta = resultMeta(entry);
        if (meta) {
          var metaEl = document.createElement("span");
          metaEl.className = "nav-search-meta";
          metaEl.textContent = meta;
          a.appendChild(metaEl);
        }
        if (entry.summary) {
          var summaryEl = document.createElement("span");
          summaryEl.className = "nav-search-summary";
          summaryEl.textContent = entry.summary;
          a.appendChild(summaryEl);
        }
        navRoot.appendChild(a);
      });
    });
  }

  function ensureLandingSearchResults() {
    if (!landingSearch) return null;
    var panel = document.getElementById("landingSearchResults");
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "landingSearchResults";
    panel.className = "landing-search-results";
    panel.hidden = true;
    var searchPanel = landingSearch.closest(".landing-search-panel");
    if (searchPanel && searchPanel.parentNode) {
      searchPanel.parentNode.insertBefore(panel, searchPanel.nextSibling);
    }
    return panel;
  }

  function renderLandingSearchResults(results, query) {
    var panel = ensureLandingSearchResults();
    if (!panel) return;
    panel.innerHTML = "";
    panel.hidden = false;
    if (!results.length) {
      var empty = document.createElement("p");
      empty.className = "landing-search-empty";
      empty.textContent = 'No documentation pages matched "' + query + '".';
      panel.appendChild(empty);
      return;
    }
    groupSearchResults(results).forEach(function (group) {
      var groupSection = document.createElement("section");
      groupSection.className = "landing-search-group";
      var heading = document.createElement("div");
      heading.className = "landing-search-group-title";
      heading.textContent = group.section;
      groupSection.appendChild(heading);
      group.entries.forEach(function (entry) {
        var a = document.createElement("a");
        a.className = "ref-card ref-row landing-search-result";
        a.href = entry.href;
        var kicker = document.createElement("span");
        kicker.className = "ref-row-kicker";
        kicker.textContent = resultMeta(entry) || "Documentation";
        var title = document.createElement("span");
        title.className = "ref-row-title";
        title.appendChild(makeDocIcon(iconKeyForPage(entry), "docs-icon-sm"));
        var titleText = document.createElement("span");
        titleText.textContent = entry.label || entry.href;
        title.appendChild(titleText);
        var summary = document.createElement("span");
        summary.className = "ref-row-summary";
        summary.textContent = entry.summary || "";
        a.appendChild(kicker);
        a.appendChild(title);
        a.appendChild(summary);
        groupSection.appendChild(a);
      });
      panel.appendChild(groupSection);
    });
  }

  /* ---------- Sidebar search filter ---------- */
  if (filterInput && navRoot) {
    var defaultNavMarkup = navRoot.innerHTML;
    filterInput.addEventListener("input", function () {
      var q = filterInput.value.trim().toLowerCase();
      if (!q) {
        navRoot.innerHTML = defaultNavMarkup;
        return;
      }
      loadSearchIndex().then(function (index) {
        renderNavSearchResults(runSearch(index, q, 14), q);
      });
    });
  }

  /* ---------- Landing-page reference filter ---------- */
  if (landingSearch) {
    var referenceSection = document.getElementById("reference");
    var groupedReferenceGroups = referenceSection
      ? Array.prototype.slice.call(referenceSection.querySelectorAll("[data-ref-group]"))
      : [];
    var referenceGroups = groupedReferenceGroups.length
      ? groupedReferenceGroups
      : (referenceSection
        ? Array.prototype.slice.call(referenceSection.querySelectorAll(".ref-grid"))
        : []);

    landingSearch.addEventListener("input", function () {
      var q = landingSearch.value.trim().toLowerCase();
      var resultsPanel = ensureLandingSearchResults();
      if (!q) {
        referenceGroups.forEach(function (group) {
          group.style.display = "";
          var heading = group.previousElementSibling;
          if (heading && heading.tagName === "H3") heading.style.display = "";
        });
        if (resultsPanel) {
          resultsPanel.hidden = true;
          resultsPanel.innerHTML = "";
        }
        if (landingSearchEmpty) landingSearchEmpty.hidden = true;
        return;
      }
      loadSearchIndex().then(function (index) {
        var results = runSearch(index, q, 12);
        referenceGroups.forEach(function (group) {
          group.style.display = "none";
          var heading = group.previousElementSibling;
          if (heading && heading.tagName === "H3") heading.style.display = "none";
        });
        renderLandingSearchResults(results, q);
        if (landingSearchEmpty) landingSearchEmpty.hidden = true;
      });
    });
  }

  /* ---------- Landing-page guide sections ---------- */
  var guideSections = Array.prototype.slice.call(document.querySelectorAll("details.guide-section"));

  function guideSectionFor(target) {
    if (!target) return null;
    if (target.matches && target.matches("details.guide-section")) return target;
    return target.closest ? target.closest("details.guide-section") : null;
  }

  function openGuideSection(target) {
    var guide = guideSectionFor(target);
    if (guide && !guide.open) guide.open = true;
    return guide;
  }

  function targetFromHash() {
    var hash = location.hash || "";
    if (!hash || hash.length < 2) return null;
    try {
      hash = decodeURIComponent(hash.slice(1));
    } catch (e) {
      hash = hash.slice(1);
    }
    return hash ? document.getElementById(hash) : null;
  }

  function revealHashTarget(shouldScroll) {
    var target = targetFromHash();
    if (!target) return;
    openGuideSection(target);
    if (shouldScroll && typeof target.scrollIntoView === "function") {
      window.requestAnimationFrame(function () {
        target.scrollIntoView({ block: "start" });
      });
    }
  }

  if (guideSections.length) {
    document.querySelectorAll("[data-guide-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var expand = btn.getAttribute("data-guide-toggle") === "expand";
        guideSections.forEach(function (guide) {
          guide.open = expand;
        });
        if (expand) revealHashTarget(false);
      });
    });

    document.addEventListener("click", function (e) {
      var anchor = e.target.closest && e.target.closest('a[href^="#"]');
      if (!anchor) return;
      var href = anchor.getAttribute("href") || "";
      if (href.length < 2) return;
      var target = document.getElementById(href.slice(1));
      if (target) openGuideSection(target);
    });

    if (location.hash) revealHashTarget(true);
    window.addEventListener("hashchange", function () {
      revealHashTarget(true);
    });
  }

  /* ---------- Quick-start tabs (landing page) ---------- */
  document.querySelectorAll("[data-tabs]").forEach(function (tabs) {
    var buttons = tabs.querySelectorAll(".tab-btn");
    var panes = tabs.querySelectorAll(".tab-pane");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var target = btn.getAttribute("data-tab");
        buttons.forEach(function (b) {
          b.setAttribute("aria-selected", b === btn ? "true" : "false");
        });
        panes.forEach(function (p) {
          p.setAttribute("data-active", p.getAttribute("data-tab") === target ? "true" : "false");
        });
      });
    });
  });

  /* ---------- Close the mobile sidebar after navigating ---------- */
  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest(".sidebar a.nav-link");
    if (link) {
      setSidebarOpen(false);
      return;
    }
    if (!sidebar || !sidebar.classList.contains("open")) return;
    var clickedSidebar = e.target.closest && e.target.closest(".sidebar");
    var clickedMenu = e.target.closest && e.target.closest(".menu-toggle");
    if (!clickedSidebar && !clickedMenu) setSidebarOpen(false);
  });
})();
