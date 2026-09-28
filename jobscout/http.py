"""PoliteFetcher: the one way Job Scout talks to other people's servers.

* realistic desktop-Chrome headers (plain requests UAs get 403s from WAFs),
* a minimum interval per host enforced across threads (3 s for company sites,
  1 s for ATS JSON APIs),
* retries with exponential backoff on 429/5xx/connection errors,
* robots.txt (cached per host) for company-site HTML pages — not for ATS APIs,
* challenge detection (403/503 + Cloudflare/Akamai markers) → `Blocked`.
"""
import logging
import threading
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

log = logging.getLogger("jobscout")

# Open-data APIs (OpenStreetMap Overpass, Census) ask clients to identify themselves; overpass-api.de
# answers 406 to generic and browser-like user agents but serves a descriptive one.
APP_UA = "JobScout/1.0 (+https://michaelwegter.com; personal job search)"

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept-Language": "en-US,en;q=0.9",
}
PAGE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ROBOTS_AGENT = "jobscout"  # robots groups for "*" apply; we identify generically

PAGE_INTERVAL = 3.0
API_INTERVAL = 1.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
CHALLENGE_MARKERS = (
    "just a moment", "cf-browser-verification", "cf-chl", "challenge-platform", "attention required",
    "_abck", "akamai", "access denied", "reference #", "px-captcha", "incapsula", "perimeterx",
    "captcha", "request unsuccessful",
)


class FetchError(Exception):
    """A request failed after retries (or returned an unexpected status)."""

    def __init__(self, url, message, status=None):
        super().__init__(f"{message} ({url})")
        self.url, self.status = url, status


class Blocked(FetchError):
    """403 or a bot challenge: plain HTTP will not get through."""


class RobotsDisallowed(FetchError):
    """robots.txt disallows this company-site page."""


def host_of(url) -> str:
    return (urlsplit(url).hostname or "").lower()


def looks_like_challenge(status, text) -> bool:
    if status not in (403, 429, 503):
        return False
    low = (text or "")[:20000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


class PoliteFetcher:
    def __init__(self, page_interval=PAGE_INTERVAL, api_interval=API_INTERVAL, timeout=25,
                 retries=3, backoff=2.0, session=None):
        self.page_interval = page_interval
        self.api_interval = api_interval
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = session or requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self._lock = threading.Lock()
        self._next_slot = {}   # host -> monotonic time the next request may start
        self._robots = {}      # scheme://host -> RobotFileParser | None (None = allow all)

    # ── spacing ──────────────────────────────────────────────────────────────
    def _wait_turn(self, host, interval):
        """Reserve the next slot for `host` under the lock, then sleep outside it."""
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = start + interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    # ── robots.txt ───────────────────────────────────────────────────────────
    def allowed_by_robots(self, url) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            cached = self._robots.get(origin, False)
        if cached is False:
            cached = self._load_robots(origin)
            with self._lock:
                self._robots[origin] = cached
        return True if cached is None else cached.can_fetch(ROBOTS_AGENT, url)

    def _load_robots(self, origin):
        self._wait_turn(host_of(origin), self.page_interval)
        try:
            resp = self.session.get(origin + "/robots.txt", timeout=10, allow_redirects=True)
        except requests.RequestException:
            return None
        if resp.status_code != 200 or "<html" in resp.text[:500].lower():
            return None  # missing/HTML robots → no restrictions
        parser = robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

    # ── requests ─────────────────────────────────────────────────────────────
    def request(self, method, url, *, kind="page", headers=None, ok_statuses=(200,), retries=None, timeout=None,
                **kwargs):
        """Polite request. kind="page" (company HTML; robots + 3 s) or "api" (ATS JSON; 1 s).
        retries/timeout override the fetcher defaults for this call.

        Returns the Response for any status in ok_statuses; raises Blocked, RobotsDisallowed
        or FetchError otherwise.
        """
        if retries is None:  # company sites get one retry; ATS APIs the full policy
            retries = min(self.retries, 1) if kind == "page" else self.retries
        if kind == "page" and not self.allowed_by_robots(url):
            raise RobotsDisallowed(url, "disallowed by robots.txt")
        interval = self.page_interval if kind == "page" else self.api_interval
        hdrs = {"Accept": PAGE_ACCEPT if kind == "page" else "application/json"}
        hdrs.update(headers or {})
        host = host_of(url)
        last_error = None
        for attempt in range(retries + 1):
            self._wait_turn(host, interval)
            try:
                resp = self.session.request(method, url, headers=hdrs, timeout=timeout or self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = FetchError(url, f"{type(exc).__name__}: {exc}")
                self._sleep_backoff(attempt)
                continue
            if resp.status_code in ok_statuses:
                return resp
            if resp.status_code == 403 or looks_like_challenge(resp.status_code, resp.text):
                raise Blocked(url, f"HTTP {resp.status_code} (blocked/challenge)", resp.status_code)
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                last_error = FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
                continue
            raise FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
        raise last_error or FetchError(url, "request failed")

    def _sleep_backoff(self, attempt, retry_after=None):
        delay = self.backoff * (2 ** attempt)
        if retry_after and str(retry_after).isdigit():
            delay = max(delay, float(retry_after))
        time.sleep(min(delay, 30.0))

    def get_page(self, url, **kwargs):
        """GET an HTML page → (text, final_url)."""
        resp = self.request("GET", url, kind="page", **kwargs)
        return resp.text, resp.url

    def get_json(self, url, **kwargs):
        return self.request("GET", url, kind="api", **kwargs).json()

    def post_json(self, url, payload, **kwargs):
        headers = {"Content-Type": "application/json", **kwargs.pop("headers", {})}
        return self.request("POST", url, kind="api", json=payload, headers=headers, **kwargs).json()


_default = None
_default_lock = threading.Lock()


def get_fetcher() -> PoliteFetcher:
    """Process-wide fetcher so per-host spacing holds across every thread."""
    global _default
    with _default_lock:
        if _default is None:
            _default = PoliteFetcher()
        return _default
