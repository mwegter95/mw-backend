"""Optional real-browser fallback (Playwright) for careers pages that block plain HTTP
or render their job widgets with JavaScript.

Playwright is imported lazily; every entry point returns None when it (or a browser
binary) is unavailable, so the rest of the pipeline degrades to "blocked/manual_check".
Uses the machine's installed Chrome (JOBS_BROWSER_CHANNEL, default "chrome") and falls
back to Playwright's bundled Chromium. One page at a time per process, and each render gets
RENDER_TIMEOUT in all: some Playwright calls (evaluate, content) have no time limit of their own, and a
render that never returned would otherwise hold the lock — and every later render — forever.
"""
import importlib.util
import logging
import threading

from . import config

log = logging.getLogger("jobscout")

_lock = threading.Lock()
RENDER_TIMEOUT = 90  # launch + load + read one page

_FALLBACK_CTX_OPTS = dict(
    viewport={"width": 1280, "height": 800},
    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    locale="en-US",
    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
)
_FALLBACK_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                         "--disable-blink-features=AutomationControlled"]

_EXTRACT_JS = """() => ({
  links: Array.from(document.querySelectorAll('a[href]')).slice(0, 800)
           .map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim().slice(0, 120)})),
  iframes: Array.from(document.querySelectorAll('iframe[src]')).map(f => f.src),
  scripts: Array.from(document.querySelectorAll('script[src]')).map(s => s.src),
})"""


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _clientfinder_opts():
    """Reuse Client Finder's realistic context/launch settings when importable."""
    try:
        import clientfinder_blueprint as cf  # mw-backend root module
        return cf._BROWSER_CTX_OPTS, cf._LAUNCH_ARGS
    except Exception:  # noqa: BLE001 — any import problem → local copy
        return _FALLBACK_CTX_OPTS, _FALLBACK_LAUNCH_ARGS


def _launch(pw, launch_args):
    channel = config.browser_channel()
    if channel and channel != "chromium":
        try:
            return pw.chromium.launch(channel=channel, headless=True, args=launch_args)
        except Exception as exc:  # noqa: BLE001 — channel missing on this machine
            log.info("browser: channel %r unavailable (%s); trying bundled chromium", channel, exc)
    return pw.chromium.launch(headless=True, args=launch_args)


def page_html(url, fetcher):
    """Static fetch first; on Blocked fall back to a rendered page once.
    Returns (html, final_url, rendered_dict|None); re-raises Blocked when rendering is impossible."""
    from .http import Blocked, get_fetcher
    fetcher = fetcher or get_fetcher()
    try:
        html, final = fetcher.get_page(url)
        return html, final, None
    except Blocked:
        rendered = fetch_rendered(url)
        if not rendered:
            raise
        return rendered["html"], rendered["final_url"], rendered


def fetch_rendered(url, timeout_ms=30000, limit=None):
    """Render `url` → {html, final_url, links, iframes, scripts, requests} or None (also when it takes longer
    than `limit` seconds, default RENDER_TIMEOUT; the stuck render is left behind on its own thread)."""
    if not available():
        return None
    limit = limit or RENDER_TIMEOUT
    if not _lock.acquire(timeout=limit * 2):
        log.warning("browser: renders are backed up; skipped %s", url)
        return None
    try:
        box = {}
        worker = threading.Thread(target=lambda: box.update(result=_render(url, timeout_ms)),
                                  name="jobs-render", daemon=True)
        worker.start()
        worker.join(limit)
        if worker.is_alive():
            log.warning("browser: render of %s took over %s s; skipped", url, limit)
            return None
        return box.get("result")
    finally:
        _lock.release()


def _render(url, timeout_ms):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    ctx_opts, launch_args = _clientfinder_opts()
    try:
        with sync_playwright() as pw:
            browser = _launch(pw, launch_args)
            try:
                ctx = browser.new_context(**ctx_opts)
                page = ctx.new_page()
                requested = []
                page.on("request", lambda r: requested.append(r.url))
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001 — long-polling pages never go idle
                    pass
                extracted = page.evaluate(_EXTRACT_JS)
                iframes = [f.url for f in page.frames if f != page.main_frame and f.url]
                return {
                    "html": page.content(),
                    "final_url": page.url,
                    "links": extracted["links"],
                    "iframes": sorted(set(iframes + extracted["iframes"])),
                    "scripts": extracted["scripts"],
                    "requests": requested[:500],
                }
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — browser missing, crash, timeout
        log.warning("browser: render failed for %s: %s", url, str(exc)[:200])
        return None
