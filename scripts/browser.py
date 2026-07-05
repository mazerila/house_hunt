"""A single shared, persistent browser context (Playwright) for fetching listing
pages. Persistent so cookies (e.g. a DataDome clearance solved once in a visible
window) survive across runs and requests.

  HOUSE_HUNT_BROWSER   firefox (default) | chrome | chromium
  HOUSE_HUNT_HEADLESS  1 (default) | 0  -> 0 shows a real window so you can solve
                       an anti-bot challenge once; the cookie is then reused.
"""
from __future__ import annotations

import asyncio

import settings

FIREFOX_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:130.0) "
              "Gecko/20100101 Firefox/130.0")
VIEWPORT = {"width": 1366, "height": 900}
# Hide the most obvious automation tell before any page script runs.
STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check", "--no-first-run",
]

_play = None
_ctx = None
_lock = asyncio.Lock()


async def _launch_context(play):
    engine = settings.BROWSER_ENGINE
    common = dict(
        user_data_dir=settings.BROWSER_PROFILE_DIR,
        headless=settings.HEADLESS,
        locale="fr-FR",
        timezone_id="Europe/Paris",
        viewport=VIEWPORT,
        extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.8,en;q=0.5"},
    )
    if engine == "firefox":
        return await play.firefox.launch_persistent_context(
            user_agent=FIREFOX_UA, **common)
    if engine == "chrome":
        return await play.chromium.launch_persistent_context(
            channel="chrome", args=_CHROMIUM_ARGS, **common)
    return await play.chromium.launch_persistent_context(
        args=_CHROMIUM_ARGS, **common)


async def get_context():
    """The shared persistent context (launched lazily)."""
    global _play, _ctx
    if _ctx is not None:
        return _ctx
    async with _lock:
        if _ctx is None:
            from playwright.async_api import async_playwright
            _play = await async_playwright().start()
            _ctx = await _launch_context(_play)
            await _ctx.add_init_script(STEALTH_JS)
    return _ctx


async def close_browser() -> None:
    global _play, _ctx
    if _ctx is not None:
        try:
            await _ctx.close()
        finally:
            _ctx = None
    if _play is not None:
        try:
            await _play.stop()
        finally:
            _play = None
