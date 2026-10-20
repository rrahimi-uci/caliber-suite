"""Capture CALIBER SPA screenshots for the overview video.

Run from the overview-video/ directory after CALIBER and MLflow are up (for
example ``make start`` from the suite root, which serves the UI at
http://127.0.0.1:5001/caliber/). The default install uses a simulated language
model and evaluator, which is fine for screenshots: the provider banner says so.
There is no demo seed in the current build, so capture against whatever
artifacts you've created; empty surfaces shoot fine too.

    cd caliber-suite/overview-video
    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/playwright install chromium
    .venv/bin/python capture_screenshots.py

The SPA signs in with a server-validated password account (the ``X-CALIBER-User``
header is ignored in the default ``session`` auth mode), so the script signs in
first. The launcher's seeded account is ``admin`` / ``admin``; override it with
``CALIBER_USERNAME`` / ``CALIBER_PASSWORD``, and point at another host with
``CALIBER_BASE_URL`` (default ``http://127.0.0.1:5001``).

Pages behind a feature flag (MCP Servers needs ``CALIBER_FEATURE_INTEGRATIONS``)
are skipped when the server reports the flag off, instead of photographing the
"disabled in this deployment" panel. Set the flag and restart CALIBER to include
them.

Output: overview-video/screenshots/*.png (dark) and screenshots/light/*.png at
1920x1080. Screenshots are pure SPA content (no browser chrome). The current
narrated deck (``docs-site/presentation.html``) is CSS/diagram-driven and does
not embed them; they are available for the docs site or a hand-cut demo.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "screenshots"
SHOTS.mkdir(exist_ok=True)

SERVER = os.environ.get("CALIBER_BASE_URL", "http://127.0.0.1:5001").rstrip("/")
# The SPA's router basename is /caliber, and so is the Vite base.
APP = f"{SERVER}/caliber"
API = f"{SERVER}/ajax-api/2.0/mlflow/caliber"
USERNAME = os.environ.get("CALIBER_USERNAME", "admin")
PASSWORD = os.environ.get("CALIBER_PASSWORD", "admin")
VIEWPORT = {"width": 1920, "height": 1080}

# Routes the current SPA serves (components/Sidebar.tsx order). Detail routes need a
# concrete id, so they're omitted here; capture them ad hoc once you have a
# workflow/run/skill id to substitute. The third field is the feature flag id the
# page sits behind (caliber/features.py), or None for an always-on page.
PAGES: list[tuple[str, str, str | None]] = [
    ("/", "caliber-dashboard.png", None),
    ("/prompts", "caliber-prompts.png", None),
    ("/eval-datasets", "caliber-test-sets.png", None),
    ("/evaluations", "caliber-evaluations.png", None),
    ("/releases", "caliber-releases.png", None),
    ("/skills", "caliber-skills.png", None),
    ("/tools", "caliber-tools.png", None),
    ("/workflows", "caliber-workflows.png", None),
    ("/knowledge-bases", "caliber-knowledge-bases.png", None),
    ("/object-store", "caliber-object-store.png", None),
    ("/observability", "caliber-observability.png", None),
    ("/gateway", "caliber-gateway.png", None),
    ("/mcp-servers", "caliber-mcp-servers.png", "integrations"),
    ("/settings", "caliber-settings.png", None),
]

# Pages that fan out several async fetches get a longer settle.
SLOW_ROUTES = ("/workflows", "/observability", "/evaluations", "/releases")


async def sign_in(page: Page) -> None:
    """Sign in through the login form and wait for the app shell."""
    await page.goto(f"{APP}/login", wait_until="domcontentloaded")
    await page.get_by_placeholder("Enter your username").fill(USERNAME)
    await page.get_by_placeholder("Enter your password").fill(PASSWORD)
    await page.get_by_role("button", name="Sign in").click()
    await page.wait_for_selector('aside[aria-label="CALIBER navigation"]', timeout=30000)


async def enabled_features(context: BrowserContext) -> dict[str, bool]:
    """Which feature flags the server reports on (``GET /capabilities``)."""
    response = await context.request.get(f"{API}/capabilities")
    if not response.ok:
        print(f"  ! /capabilities answered {response.status}; treating every flag as off")
        return {}
    features = (await response.json()).get("data", {}).get("features", {})
    return {fid: bool(info.get("enabled")) for fid, info in features.items()}


async def capture_set(p, *, theme: str, out_dir: Path) -> None:
    """Capture every page once with the given theme applied at load time."""
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n-- Capturing {theme} mode -> {out_dir.name}/ --")
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    # Seed localStorage *before* the SPA boots so useTheme picks it up
    # on first render: no flash-of-wrong-theme between mount and toggle.
    await context.add_init_script(
        f"window.localStorage.setItem('caliber.theme', {theme!r});"
    )
    page = await context.new_page()
    await sign_in(page)
    flags = await enabled_features(context)
    for route, filename, feature in PAGES:
        if feature and not flags.get(feature, False):
            print(f"  - skip {route}: feature '{feature}' is off on this server")
            continue
        url = f"{APP}{route}"
        print(f"  -> {url}")
        # The SPA opens persistent SSE streams, so ``networkidle`` never fires.
        await page.goto(url, wait_until="domcontentloaded")
        # The TopBar renders on every authenticated page with the CALIBER
        # wordmark: a stable marker that React rendered the shell.
        await page.wait_for_selector("header :text('CALIBER')", timeout=15000)
        await page.wait_for_timeout(1500 if route in SLOW_ROUTES else 900)
        out = out_dir / filename
        await page.screenshot(path=str(out), full_page=False)
        print(f"    saved {out.name}")
    await context.close()
    await browser.close()


async def main() -> None:
    async with async_playwright() as p:
        # Dark-mode screenshots go to the root screenshots/ dir.
        await capture_set(p, theme="dark", out_dir=SHOTS)
        # Light-mode screenshots land in a side dir so they're available
        # if the docs site ever wants to show before/after.
        await capture_set(p, theme="light", out_dir=SHOTS / "light")


if __name__ == "__main__":
    asyncio.run(main())
