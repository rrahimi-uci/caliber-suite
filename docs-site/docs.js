/*
 * CALIBER docs — shared client behaviour for the landing page and every
 * generated module page: theme toggle, Mermaid rendering, the DOCS_NAV-driven
 * sidebar, a per-page table of contents with scroll-spy, sidebar search, and
 * the quick-start tabs. Loaded with `defer` after `docs-nav.js`.
 */
(function () {
  "use strict";
  var docRoot = document.documentElement;

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
  var currentPage = (location.pathname.split("/").pop() || "index.html").toLowerCase();
  if (!currentPage || currentPage === "") currentPage = "index.html";

  function pageOf(href) {
    var p = (href || "").split("#")[0].split("/").pop().toLowerCase();
    return p || "index.html";
  }

  function setSidebarOpen(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", Boolean(open));
    if (menuToggle) menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function sectionLabel(sec) {
    return sec && sec.section === "Documentation" ? "Overview" : (sec && sec.section) || "";
  }

  function sectionHref(sec) {
    return sec && sec.links && sec.links[0] && sec.links[0].href ? sec.links[0].href : "index.html";
  }

  function sectionIsCurrent(sec) {
    return Boolean((sec && sec.links || []).some(function (lnk) {
      return pageOf(lnk.href) === currentPage;
    }));
  }

  function sectionSummary(sec) {
    switch (sectionLabel(sec)) {
      case "Overview":
        return "Start here for the product story, quickstart, and documentation paths.";
      case "Platform docs":
        return "Architecture, runtime behavior, governance, and operations.";
      case "REST API":
        return "The raw HTTP contract, auth conventions, and route families.";
      case "Python SDK":
        return "Typed client entry points, async support, plugins, and examples.";
      case "Examples":
        return "Cookbooks, walkthroughs, and runnable end-to-end implementation patterns.";
      case "Strategy & roadmap":
        return "Competitive positioning, future direction, and planning context.";
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

  if (navRoot && Array.isArray(window.DOCS_NAV)) {
    var frag = document.createDocumentFragment();
    window.DOCS_NAV.forEach(function (sec) {
      var head = document.createElement("div");
      head.className = "nav-section";
      head.textContent = sec.section;
      frag.appendChild(head);
      (sec.links || []).forEach(function (lnk) {
        var a = document.createElement("a");
        a.className = "nav-link";
        a.href = lnk.href;
        a.textContent = lnk.label;
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
          a.appendChild(mark);
        } else if (pageOf(lnk.href) === currentPage) {
          a.classList.add("active");
        }
        frag.appendChild(a);
      });
    });
    navRoot.appendChild(frag);
  }

  if (sectionTabsRoot && Array.isArray(window.DOCS_NAV)) {
    var currentSection = null;
    window.DOCS_NAV.forEach(function (sec) {
      if (!currentSection && sectionIsCurrent(sec)) currentSection = sec;
    });
    if (!currentSection) currentSection = window.DOCS_NAV[0] || null;

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
    window.DOCS_NAV.forEach(function (sec) {
      if (!sec || !sec.links || !sec.links.length) return;
      var item = document.createElement("a");
      item.className = "docs-browse-item";
      item.href = sectionHref(sec);
      if (sectionIsCurrent(sec)) item.classList.add("active");

      var title = document.createElement("span");
      title.className = "docs-browse-item-title";
      title.textContent = sectionLabel(sec);

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

    if (currentSection) {
      var activeSection = document.createElement("a");
      activeSection.className = "topbar-pill topbar-active-section";
      activeSection.href = sectionHref(currentSection);
      activeSection.textContent = sectionLabel(currentSection);
      activeSection.setAttribute("aria-label", "Current section: " + sectionLabel(currentSection));
      topbarFrag.appendChild(activeSection);
    }

    sectionTabsRoot.appendChild(topbarFrag);
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
    if (!Array.isArray(window.DOCS_NAV)) return;
    var pages = flattenPages(window.DOCS_NAV);
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
    meta.textContent = "Page " + String(idx + 1) + " of " + String(pages.length) + " \u00b7 Alt+\u2190 / Alt+\u2192";
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

  /* ---------- Per-page table of contents ---------- */
  // Module pages tag their <h2> with ids; the landing page uses <section id>.
  var content = document.querySelector(".content");
  var tocTargets = [];
  if (content) {
    var taggedH2 = content.querySelectorAll("h2[id]");
    if (taggedH2.length) {
      tocTargets = Array.prototype.slice.call(taggedH2).map(function (h) {
        return { id: h.id, text: h.textContent.replace(/#\s*$/, "").trim(), el: h };
      });
    } else {
      var secs = content.querySelectorAll("section[id]");
      tocTargets = Array.prototype.slice
        .call(secs)
        .filter(function (s) { return s.querySelector("h2"); })
        .map(function (s) {
          return { id: s.id, text: s.querySelector("h2").textContent.trim(), el: s };
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

  /* ---------- Sidebar search filter ---------- */
  if (filterInput && navRoot) {
    filterInput.addEventListener("input", function () {
      var q = filterInput.value.trim().toLowerCase();
      var groups = [];
      var g = null;
      Array.prototype.slice.call(navRoot.children).forEach(function (el) {
        if (el.classList.contains("nav-section")) {
          g = { header: el, links: [] };
          groups.push(g);
        } else if (el.classList.contains("nav-link") && g) {
          g.links.push(el);
        }
      });
      groups.forEach(function (grp) {
        var visible = 0;
        grp.links.forEach(function (a) {
          var show = !q || a.textContent.toLowerCase().indexOf(q) !== -1;
          a.style.display = show ? "" : "none";
          if (show) visible++;
        });
        grp.header.style.display = visible ? "" : "none";
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
      var visibleCards = 0;

      referenceGroups.forEach(function (group) {
        var cards = Array.prototype.slice.call(group.querySelectorAll(".ref-card"));
        var groupVisible = 0;

        cards.forEach(function (card) {
          var text = (card.textContent || "").trim().toLowerCase();
          var show = !q || text.indexOf(q) !== -1;
          card.style.display = show ? "" : "none";
          if (show) {
            groupVisible += 1;
            visibleCards += 1;
          }
        });

        if (group.hasAttribute && group.hasAttribute("data-ref-group")) {
          group.style.display = groupVisible ? "" : "none";
        } else {
          group.style.display = groupVisible ? "" : "none";
          var heading = group.previousElementSibling;
          if (heading && heading.tagName === "H3") {
            heading.style.display = groupVisible ? "" : "none";
          }
        }
      });

      if (landingSearchEmpty) landingSearchEmpty.hidden = visibleCards !== 0;
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
