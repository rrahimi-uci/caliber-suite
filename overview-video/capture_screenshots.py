"""Capture CALIBER SPA screenshots for the overview video.

Run from the overview-video/ directory after MLflow + CALIBER is up locally
(object storage reachable, a real LLM provider key exported). There is no demo
seed in the current build, so capture against whatever artifacts you've created
(by hand or via Aria); empty surfaces shoot fine too.

    cd caliber-suite/overview-video
    .venv/bin/python capture_screenshots.py

Output: overview-video/screenshots/*.png at 1920×1080. Screenshots are pure SPA
content (no browser chrome) — they look identical to what a reviewer sees
in the deployed app.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Page, async_playwright

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "screenshots"
SHOTS.mkdir(exist_ok=True)

BASE = "http://127.0.0.1:5001/caliber"
VIEWPORT = {"width": 1920, "height": 1080}
HEADERS = {"X-CALIBER-User": "@local-admin"}


async def shot(page: Page, route: str, filename: str, *, settle_ms: int = 1500) -> None:
    """Navigate to /caliber{route}, wait for the page to settle, screenshot.

    The SPA opens persistent SSE streams so ``networkidle`` never fires.
    We wait for the top bar to mount (a stable marker that React rendered)
    and give async page data fetches an extra moment to populate.
    """
    url = f"{BASE}/caliber{route}"
    print(f"  → {url}")
    await page.goto(url, wait_until="domcontentloaded")
    # The TopBar renders unconditionally on every page, with the CALIBER
    # wordmark — using it as the readiness signal keeps us out of "wait
    # for a route-specific selector" land.
    await page.wait_for_selector("header :text('CALIBER')", timeout=15000)
    await page.wait_for_timeout(settle_ms)
    out = SHOTS / filename
    await page.screenshot(path=str(out), full_page=False)
    print(f"    saved {out.name}")


# Routes the current SPA actually serves (sidebar order). Detail routes need a
# concrete id, so they're omitted here — capture them ad hoc once you have a
# workflow/run/skill id to substitute.
PAGES: list[tuple[str, str]] = [
    ("/", "caliber-dashboard.png"),
    ("/workflows", "caliber-workflows.png"),
    ("/prompts", "caliber-prompts.png"),
    ("/tools", "caliber-tools.png"),
    ("/skills", "caliber-skills.png"),
    ("/mcp-servers", "caliber-mcp-servers.png"),
    ("/knowledge-bases", "caliber-knowledge-bases.png"),
    ("/object-store", "caliber-object-store.png"),
    ("/eval-datasets", "caliber-test-sets.png"),
    ("/observability", "caliber-observability.png"),
    ("/evaluations", "caliber-evaluations.png"),
    ("/gateway", "caliber-gateway.png"),
    ("/settings", "caliber-settings.png"),
]


async def capture_set(p, *, theme: str, out_dir: Path) -> None:
    """Capture every page once with the given theme applied at load time."""
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n── Capturing {theme} mode → {out_dir.name}/ ──")
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport=VIEWPORT,
        extra_http_headers=HEADERS,
        device_scale_factor=1,
    )
    # Seed localStorage *before* the SPA boots so useTheme picks it up
    # on first render — no flash-of-wrong-theme between mount and toggle.
    await context.add_init_script(
        f"window.localStorage.setItem('caliber.theme', {theme!r});"
    )
    page = await context.new_page()
    for route, filename in PAGES:
        url = f"{BASE}/caliber{route}"
        print(f"  → {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("header :text('CALIBER')", timeout=15000)
        # Slightly longer settle for pages that fan out multiple async fetches.
        await page.wait_for_timeout(
            1500 if route in ("/workflows", "/observability", "/evaluations") else 900
        )
        out = out_dir / filename
        await page.screenshot(path=str(out), full_page=False)
        print(f"    saved {out.name}")
    await context.close()
    await browser.close()


async def main() -> None:
    async with async_playwright() as p:
        # Dark-mode screenshots go to the root screenshots/ dir — these
        # are what the presentation deck embeds in its SPA-tour scenes.
        await capture_set(p, theme="dark", out_dir=SHOTS)
        # Light-mode screenshots land in a side dir so they're available
        # if the docs site ever wants to show before/after.
        await capture_set(p, theme="light", out_dir=SHOTS / "light")


if __name__ == "__main__":
    asyncio.run(main())
