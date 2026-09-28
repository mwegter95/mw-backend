"""Find a company's careers page and identify the system behind it.

Order: known careers_url → homepage (links whose text/href look like careers) → common paths
(/careers, /jobs, …) → sitemap.xml, following one hop from a careers page to "view openings"
style links. Static HTML first; a rendered browser page when blocked or when the careers page
is JavaScript-only with no ATS found. `detect(company)` returns the company fields to update.
"""
import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from . import ats, browser, db
from .http import Blocked, FetchError, get_fetcher
from .normalize import html_to_text

log = logging.getLogger("jobscout")

_CAREER_TEXT = re.compile(r"\b(careers?|jobs|join (our )?team|join us|work (with|for) us|employment|"
                          r"opportunities|we'?re hiring|open positions)\b", re.I)
_CAREER_HREF = re.compile(r"(career|/jobs?\b|join-?us|join-our-team|employment|opportunit|work-(with|for)-us|hiring)", re.I)
_FOLLOW_TEXT = re.compile(r"((search|view|see|browse|explore|find)\s+(all\s+)?(current\s+)?(jobs|openings|positions|"
                          r"opportunities|careers)|current openings|open positions|job openings|job search|apply now)", re.I)
_JOBS_WORDS = re.compile(r"\b(jobs?|careers?|positions?|openings?|apply|hiring)\b", re.I)
COMMON_PATHS = ["/careers", "/jobs", "/careers/", "/about/careers", "/company/careers", "/about-us/careers",
                "/join-us", "/work-with-us", "/employment"]
MAX_PAGES = 7
_REAL_ATS_EXCLUDE = {"jsonld", "phenom"}


def career_links(html, base_url):
    """Links on a page that look like a careers entry point, best first."""
    soup = BeautifulSoup(html or "", "html.parser")
    scored = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if not href.startswith("http"):
            continue
        text = a.get_text(" ", strip=True)[:80]
        score = (2 if _CAREER_TEXT.search(text) else 0) + (1 if _CAREER_HREF.search(href) else 0)
        if ats.detect_from_url(href):
            score += 3
        if score:
            scored.append((score, href))
    scored.sort(key=lambda s: -s[0])
    return list(dict.fromkeys(h for _, h in scored))[:6]


def follow_links(html, base_url):
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if href.startswith("http") and (_FOLLOW_TEXT.search(a.get_text(" ", strip=True)) or ats.detect_from_url(href)):
            out.append(href)
    return list(dict.fromkeys(out))[:2]


def _sitemap_links(probe, origin):
    got = probe.fetch(origin + "/sitemap.xml")
    if not got:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", got[0])
    return [u for u in locs if re.search(r"career|/jobs?\b", u, re.I)][:2]


def _is_careers_page(url, html) -> bool:
    """Guard against soft-404s: the URL or the page title/h1 must talk about careers/jobs."""
    if urlsplit(url).path.strip("/") == "":
        return False
    soup = BeautifulSoup(html or "", "html.parser")
    heading = " ".join(t.get_text(" ", strip=True) for t in soup.find_all(["title", "h1"])[:3])
    if _CAREER_TEXT.search(heading):
        return True
    body = soup.get_text(" ", strip=True)[:20000]
    return bool(_CAREER_HREF.search(urlsplit(url).path) and _JOBS_WORDS.search(body))


def _looks_js_rendered(html) -> bool:
    return len(html_to_text(html)) < 1200


def _outcome(found, careers_url=None, reason=None):
    fields = {"ats_detected_at": db.now_iso(), "consecutive_failures": 0, "last_error": None}
    if found:
        ats_type = found["ats_type"]
        fields.update({k: found[k] for k in ("ats_type", "ats_host", "ats_key", "ats_site")})
        fields["careers_url"] = found.get("careers_url") or careers_url
        # Without an adapter for this careers system the board page itself is rendered and the AI reads
        # the jobs off it — the same path as a plain careers page — so the company still gets swept.
        reason = None if ats.has_adapter(ats_type) else f"{ats_type} board read by AI (no adapter yet)"
        fields.update(status="active", status_reason=reason)
    else:
        fields.update(careers_url=careers_url, status="no_careers", status_reason=reason or "no careers page found")
    return fields


class _Probe:
    """One detection attempt for one company (keeps track of what was fetched)."""

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.fetched = set()
        self.dead_hosts = set()  # connection/TLS failures: don't probe the same host again
        self.blocked_url = None
        self.careers_page = None  # (url, html)

    def fetch(self, url):
        host = urlsplit(url).hostname
        if url in self.fetched or host in self.dead_hosts or len(self.fetched) >= MAX_PAGES:
            return None
        self.fetched.add(url)
        try:
            return self.fetcher.get_page(url)
        except Blocked:
            self.blocked_url = self.blocked_url or url
        except FetchError as exc:
            if exc.status is None:
                self.dead_hosts.add(host)
            log.debug("careers: %s", exc)
        return None

    def check(self, url, depth=0):
        """Fetch url; return an ATS result or None (remembering the careers page for later)."""
        direct = ats.detect_from_url(url)
        if direct and direct["ats_type"] not in _REAL_ATS_EXCLUDE:
            return direct
        got = self.fetch(url)
        if not got:
            return None
        html, final = got
        found = ats.detect_from_html(html, final)
        if found and found["ats_type"] not in _REAL_ATS_EXCLUDE:
            return found
        if found or (self.careers_page is None and _is_careers_page(final, html)):
            self.careers_page = (final, html, found)
        if depth == 0:
            for nxt in follow_links(html, final):
                hit = self.check(nxt, depth=1)
                if hit:
                    return hit
        return None


def detect(company, fetcher=None, homepage=None, use_browser=True):
    """Find careers page + ATS for a company row/dict → dict of company fields to update.
    `homepage` may carry an already-fetched (html, final_url) to save a request."""
    probe = _Probe(fetcher or get_fetcher())
    home_url = company.get("homepage_url") or f"https://{company['domain']}"
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(home_url))

    if company.get("careers_url"):
        hit = probe.check(company["careers_url"])
        if hit:
            return _outcome(hit)
    if homepage is None:
        homepage = probe.fetch(home_url)
    else:
        probe.fetched.add(home_url)
    candidates = []
    if homepage:
        html, final = homepage
        found = ats.detect_from_html(html, final)
        if found and found["ats_type"] not in _REAL_ATS_EXCLUDE:
            return _outcome(found)
        candidates += career_links(html, final)
    candidates += [origin + p for p in COMMON_PATHS]
    for url in candidates:
        hit = probe.check(url)
        if hit:
            return _outcome(hit)
        if probe.careers_page:
            break
    if not probe.careers_page:
        for url in _sitemap_links(probe, origin):
            hit = probe.check(url)
            if hit:
                return _outcome(hit)

    rendered_target = None
    if probe.careers_page and _looks_js_rendered(probe.careers_page[1]) and not probe.careers_page[2]:
        rendered_target = probe.careers_page[0]
    elif probe.blocked_url and not probe.careers_page:
        rendered_target = probe.blocked_url
    if use_browser and rendered_target:
        rendered = browser.fetch_rendered(rendered_target)
        if rendered:
            extra = rendered["iframes"] + rendered["scripts"] + rendered["requests"] + [l["href"] for l in rendered["links"]]
            found = ats.detect_from_html(rendered["html"], rendered["final_url"], extra)
            if found:
                return _outcome(found)
            probe.careers_page = (rendered["final_url"], rendered["html"], None)
        elif probe.blocked_url and not probe.careers_page:
            reason = "blocked (403/challenge); browser fallback unavailable" if not browser.available() \
                else "blocked (403/challenge) even in browser"
            fields = _outcome(None)
            fields.update(status="manual_check" if not browser.available() else "blocked", status_reason=reason,
                          careers_url=probe.blocked_url)
            return fields

    if probe.careers_page:
        url, html, found = probe.careers_page
        if found:  # phenom / jsonld
            return _outcome(found, url)
        fields = _outcome({"ats_type": "html", "ats_host": urlsplit(url).hostname, "ats_key": None,
                           "ats_site": None, "careers_url": url})
        fields.update(status="active", status_reason="jobs read by AI from the careers page")
        return fields
    if probe.blocked_url:
        fields = _outcome(None)
        fields.update(status="blocked", status_reason="blocked (403/challenge)", careers_url=probe.blocked_url)
        return fields
    return _outcome(None)
