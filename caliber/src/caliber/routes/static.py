"""Serve the CALIBER SPA from the Python plugin.

The Vite build lands in ``src/caliber/ui/`` (CI copies ``caliber-ui/dist/*``
there before building the wheel; Hatchling's ``force-include`` directive
ships the directory inside the installed package). This module exposes
two routes under ``/caliber``:

* ``GET /caliber``, ``GET /caliber/``, and ``GET /caliber/{path:path}`` — serve the asset
  if it exists, else fall back to ``index.html`` so the SPA's client-
  side router handles the rest. (The history-mode fallback the React
  Router needs.)
* Any non-asset path *under* ``/caliber/`` re-serves the index, so a
  deep link like ``/caliber/approvals/AP-123`` opens the SPA correctly
  on a hard refresh.

At serve time we inject ``window.__CALIBER_STATIC_PREFIX__`` into the
``<head>`` of ``index.html`` so the SPA can build prefix-aware API URLs
and router basenames when MLflow runs behind a reverse-proxy subpath.
The patched index is cached after first read (it doesn't change for
the lifetime of the process); other assets stream directly from disk
via :class:`FileResponse`.

Security: every path is resolved against the UI directory and rejected
if the resolved location isn't inside it. That blocks the classic
``../../etc/passwd`` traversal even though Starlette also normalizes
the path.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlencode, urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from caliber.config import CaliberConfig

logger = logging.getLogger("caliber.routes.static")

# The SPA mount point. MLflow's static-prefix is a *separate* concern —
# when it's set the SPA still lives at ``{prefix}/caliber`` and the API
# at ``{prefix}/ajax-api/...``. Starlette doesn't see the prefix on
# inbound paths (MLflow strips it before dispatch), so the route
# definitions here are prefix-free.
ROOT_PATH = "/"
SPA_BARE_PATH = "/caliber"
SPA_ROOT_PATH = "/caliber/"
SPA_DEEP_PATH = "/caliber/{path:path}"
_DEFAULT_MLFLOW_URL = "http://127.0.0.1:5000"

# The package directory the built SPA lands in. CI copies the Vite
# build output (``caliber-ui/dist/*``) here before the Python wheel is
# built. In a `pip install -e .` workflow without a build step the
# directory may not exist; the routes degrade gracefully.
_DEFAULT_UI_DIR = Path(__file__).resolve().parent.parent / "ui"

# Cache policy. The SPA shell (index.html) references content-hashed chunk
# filenames, so a browser-cached *stale shell* pins the user to old chunks —
# the recurring "I hard-refreshed and still see the old UI" bug. Serve the
# shell with ``no-cache`` (always revalidate, so a redeploy is picked up on the
# next navigation) and the hashed ``assets/`` files as immutable (the filename
# changes whenever the content does, so a year-long cache is always safe).
_HTML_CACHE_CONTROL = "no-cache"
_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _cache_control_for(relative: str) -> str:
    """Immutable for content-hashed ``assets/`` files; revalidate everything else."""
    return (
        _ASSET_CACHE_CONTROL if relative.lstrip("/").startswith("assets/") else _HTML_CACHE_CONTROL
    )


class StaticUIHandler:
    """Encapsulates the per-app state the static routes need.

    Built once at startup with the resolved UI dir + the runtime
    static-prefix. The two route handlers below close over a single
    instance, parked on ``app.state`` for tests that want to swap the
    UI dir for a temp directory.
    """

    def __init__(self, ui_dir: Path, static_prefix: str) -> None:
        self._ui_dir = ui_dir.resolve()
        self._static_prefix = static_prefix.rstrip("/")
        self._cached_index: str | None = None
        self._cached_index_mtime_ns: int | None = None

    @property
    def ui_dir(self) -> Path:
        return self._ui_dir

    def index_html(self) -> str:
        """Return the prefix-injected index.html, lazily cached.

        Raises :class:`FileNotFoundError` if the UI bundle isn't on disk;
        the route handler translates that into a 503-with-instructions
        so the operator knows the dev server needs to be wired up.
        """
        path = self._ui_dir / "index.html"
        if not path.is_file():
            raise FileNotFoundError(f"SPA index.html not found at {path}")
        mtime_ns = path.stat().st_mtime_ns
        if self._cached_index is not None and self._cached_index_mtime_ns == mtime_ns:
            return self._cached_index
        raw = path.read_text(encoding="utf-8")
        self._cached_index = _inject_prefix(raw, self._static_prefix)
        self._cached_index_mtime_ns = mtime_ns
        return self._cached_index

    def resolve_asset(self, relative: str) -> Path | None:
        """Resolve a request path to an on-disk asset under ``ui_dir``.

        Returns ``None`` if the asset doesn't exist or escapes the
        sandbox; callers fall back to serving the index in that case.
        """
        if relative.startswith("/"):
            relative = relative[1:]
        if not relative:
            return None
        # Resolve and re-anchor under ui_dir. ``Path.resolve()`` collapses
        # `..` segments so a traversal attempt resolves outside the dir
        # and the containment check below catches it.
        candidate = (self._ui_dir / relative).resolve()
        try:
            candidate.relative_to(self._ui_dir)
        except ValueError:
            logger.warning("rejected SPA asset path outside ui_dir: %s", relative)
            return None
        if not candidate.is_file():
            return None
        return candidate


def _inject_prefix(html: str, prefix: str) -> str:
    """Splice ``window.__CALIBER_STATIC_PREFIX__`` into ``<head>``.

    We use plain string substitution rather than parsing the HTML — the
    Vite-built ``index.html`` is small and well-formed, and a parser
    dependency would dwarf this module. The injection is idempotent:
    re-running on the patched output is a no-op.
    """
    if "__CALIBER_STATIC_PREFIX__" in html:
        return html
    snippet = f"<script>window.__CALIBER_STATIC_PREFIX__={json.dumps(prefix)};</script>"
    # Prefer to put the script just before `</head>` so it runs before
    # the bundled JS (which Vite emits at the end of <body>). Fall back
    # to prepending when no </head> is present (defensive — shouldn't
    # happen with the stock Vite template).
    needle = "</head>"
    if needle in html:
        return html.replace(needle, f"{snippet}{needle}", 1)
    return snippet + html


def _resolved_mlflow_url() -> str:
    """Return the configured MLflow URL (or a safe localhost default).

    ``/?ui=mlflow`` on the direct CALIBER port uses this so users can jump
    to MLflow without the gateway. We accept only absolute ``http(s)`` URLs;
    malformed values fall back to localhost.
    """
    raw = (os.environ.get("MLFLOW_URL") or _DEFAULT_MLFLOW_URL).strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw.rstrip("/")
    logger.warning("invalid MLFLOW_URL=%r; falling back to %s", raw, _DEFAULT_MLFLOW_URL)
    return _DEFAULT_MLFLOW_URL


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _get_handler(request: Request) -> StaticUIHandler:
    handler: StaticUIHandler = request.app.state.static_ui_handler
    return handler


def _missing_ui_response() -> Response:
    """503 with operator-facing instructions when the SPA bundle is absent.

    Distinct from a generic 404 so an operator who hits ``/caliber/`` on
    a fresh install knows the next step is to either install a published
    wheel (which includes ``ui/``) or run the SPA dev server.
    """
    body = (
        "<!doctype html><html><head><title>CALIBER SPA not bundled</title>"
        '<meta charset="utf-8"/></head><body style="font-family:Inter,system-ui,sans-serif;'
        'max-width:640px;margin:48px auto;padding:0 16px;color:#111;">'
        "<h1 style='font-size:18px;'>CALIBER SPA not bundled</h1>"
        "<p>This install of <code>caliber</code> doesn't ship the built UI assets. "
        "Either install a release wheel (which includes them) or run the dev server "
        "from <code>caliber-ui/</code>:</p>"
        "<pre style='background:#f4f4f5;padding:12px;border-radius:8px;font-size:13px;'>"
        "cd caliber-ui &amp;&amp; npm install &amp;&amp; npm run dev</pre>"
        "<p style='color:#666;font-size:13px;'>API endpoints under "
        "<code>/ajax-api/2.0/mlflow/caliber/*</code> work regardless of UI bundling.</p>"
        "</body></html>"
    )
    return HTMLResponse(body, status_code=503)


async def serve_index(request: Request) -> Response:
    """Serve the SPA shell (``index.html``).

    Routed for the bare ``/caliber/`` entry point and used as the
    history-mode fallback for unknown deep paths.
    """
    handler = _get_handler(request)
    try:
        return HTMLResponse(handler.index_html(), headers={"Cache-Control": _HTML_CACHE_CONTROL})
    except FileNotFoundError:
        return _missing_ui_response()


async def serve_root(request: Request) -> Response:
    """``GET /`` entrypoint for direct CALIBER service access.

    Default behavior sends users to ``/caliber/``. The ``?ui=mlflow`` sentinel
    redirects to MLflow so links like ``http://127.0.0.1:5001/?ui=mlflow`` work
    even without the unified gateway.
    """
    if request.query_params.get("ui") == "mlflow":
        target = _resolved_mlflow_url() + "/"
        extra = [(k, v) for k, v in request.query_params.multi_items() if k != "ui"]
        if extra:
            target = f"{target}?{urlencode(extra, doseq=True)}"
        return RedirectResponse(url=target, status_code=302)
    return RedirectResponse(url=SPA_ROOT_PATH, status_code=302)


async def serve_path(request: Request) -> Response:
    """Serve an asset under ``/caliber/<path>`` — or the SPA shell as fallback.

    Two cases:
      1. ``path`` matches a real file in the UI dir → stream it via
         :class:`FileResponse` (Starlette handles ``Content-Type`` and
         conditional GET via ETag headers).
      2. ``path`` doesn't match a file → render the SPA shell. React
         Router on the client side resolves the URL to the right page.

    The fallback is what makes a hard refresh of ``/caliber/approvals/AP-1``
    work without a 404. Without it the user would see Starlette's
    default 404 page when they bookmark a deep link.
    """
    handler = _get_handler(request)
    raw_path = request.path_params.get("path", "")
    asset = handler.resolve_asset(raw_path)
    if asset is not None:
        return FileResponse(asset, headers={"Cache-Control": _cache_control_for(raw_path)})
    # No asset — serve the SPA shell so the client-side router handles
    # the URL. This is the standard SPA history-mode fallback.
    try:
        return HTMLResponse(handler.index_html(), headers={"Cache-Control": _HTML_CACHE_CONTROL})
    except FileNotFoundError:
        return _missing_ui_response()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def build_handler(config: CaliberConfig) -> StaticUIHandler:
    """Construct the handler used by both routes.

    Reads the static prefix from the resolved config so a deployment
    behind ``MLFLOW_STATIC_PREFIX=/mlflow`` injects ``"/mlflow"`` into
    the served HTML.
    """
    return StaticUIHandler(_DEFAULT_UI_DIR, config.static_prefix)


def register(app: Starlette) -> None:
    """Add the SPA routes to the given Starlette application.

    ``server.create_app`` parks the handler on ``app.state`` before
    calling this so the route handlers can pick it up.
    """
    if not hasattr(app.state, "static_ui_handler"):
        raise RuntimeError(
            "static_ui_handler missing from app.state; "
            "create_app must build it before calling register_routes."
        )
    app.routes.append(Route(ROOT_PATH, serve_root, methods=["GET", "HEAD"]))
    app.routes.append(Route(SPA_BARE_PATH, serve_index, methods=["GET"]))
    app.routes.append(Route(SPA_ROOT_PATH, serve_index, methods=["GET"]))
    app.routes.append(Route(SPA_DEEP_PATH, serve_path, methods=["GET"]))


def _missing_ui_response_for_tests() -> Response:
    """Public-from-tests alias so tests can assert on the response body
    shape without re-importing the underscore-prefixed function."""
    return _missing_ui_response()
