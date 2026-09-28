"""ATS registry and detection.

`detect_from_url(url)` / `detect_from_html(html, base_url)` recognise every ats_type in the
contract from hostnames and URL shapes found in links, iframes, scripts and inline JSON,
falling back to Phenom markers and schema.org JobPosting JSON-LD ("jsonld").
`get_adapter(ats_type)` returns an adapter instance or raises NotImplementedError.
"""
import json
import re
from collections import deque
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter, RawJob

__all__ = ["Adapter", "RawJob", "detect_from_url", "detect_from_html", "extract_jsonld_jobs",
           "extract_jsonld_orgs", "get_adapter", "has_adapter"]

_GENERIC_SUBDOMAINS = {"www", "api", "app", "apps", "help", "support", "status", "blog", "marketing", "cdn",
                       "static", "assets", "info", "go", "community", "developer", "developers", "docs",
                       "apply", "jobs", "careers", "login", "secure", "partners", "resources", "hr", "news",
                       "press", "get", "try", "learn", "university", "academy", "events", "media", "images",
                       "img", "files", "content", "click", "email", "mail", "links", "link", "track"}
_ID = r"[A-Za-z0-9_.-]+"
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _result(ats_type, host=None, key=None, site=None, careers_url=None):
    return {"ats_type": ats_type, "ats_host": host, "ats_key": key, "ats_site": site,
            "careers_url": careers_url}


def _workday(m, _url):
    host = m.group("host").lower()
    return _result("workday", host, m.group("tenant").lower(), m.group("site"), f"https://{host}/{m.group('site')}")


def _workday_site(m, _url):
    host = m.group("host").lower()
    tenant, site = m.group("tenant"), m.group("site")
    return _result("workday", host, tenant, site, f"https://{host}/recruiting/{tenant}/{site}")


def _oracle(m, _url):
    host, site = m.group("host").lower(), m.group("site")
    return _result("oracle", host, host.split(".")[0], site,
                   f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}")


def _lever(m, _url):
    host = "jobs.eu.lever.co" if m.groupdict().get("eu") else "jobs.lever.co"
    return _result("lever", host, m.group("key"), None, f"https://{host}/{m.group('key')}")


def _simple(ats_type, careers_fmt, key_group="key"):
    def build(m, url):
        key = m.group(key_group)
        groups = m.groupdict()
        host = (groups.get("host") or urlsplit(url).hostname or "").lower()
        careers = careers_fmt.format(key=key, host=host, **{k: v for k, v in groups.items() if k not in ("key", "host")}) \
            if careers_fmt else url
        return _result(ats_type, host, key, groups.get("site"), careers)
    return build


# (ats_type, pattern, builder) in priority order: readable JSON ATSs first.
_MATCHERS = [
    ("workday", r"https?://(?P<host>(?P<tenant>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com)/"
                r"(?:wday/cxs/[a-z0-9_-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>(?!wday\b)[A-Za-z0-9_-]+)", _workday),
    ("workday", r"https?://(?P<host>wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/"
                r"(?P<tenant>[A-Za-z0-9_-]+)/(?P<site>[A-Za-z0-9_-]+)", _workday_site),
    ("oracle", r"https?://(?P<host>[a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
               r"(?:[a-z]{2}(?:-[A-Z]{2})?/)?sites/(?P<site>[A-Za-z0-9_-]+)", _oracle),
    ("oracle", r"https?://(?P<host>[a-z0-9.-]+\.oraclecloud\.com)/hcmRestApi/resources/[^\s\"']*?"
               r"siteNumber=(?P<site>[A-Za-z0-9_-]+)", _oracle),
    ("greenhouse", r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?"
                   r"(?P<key>(?!embed\b|v1\b)[A-Za-z0-9_-]+)",
     _simple("greenhouse", "https://job-boards.greenhouse.io/{key}")),
    ("greenhouse", r"https?://boards-api\.greenhouse\.io/v1/boards/(?P<key>[A-Za-z0-9_-]+)",
     _simple("greenhouse", "https://job-boards.greenhouse.io/{key}")),
    ("lever", r"https?://jobs\.(?P<eu>eu\.)?lever\.co/(?P<key>[A-Za-z0-9_.-]+)", _lever),
    ("lever", r"https?://api\.(?P<eu>eu\.)?lever\.co/v0/postings/(?P<key>[A-Za-z0-9_.-]+)", _lever),
    ("ashby", r"https?://jobs\.ashbyhq\.com/(?P<key>[A-Za-z0-9_.%-]+)",
     _simple("ashby", "https://jobs.ashbyhq.com/{key}")),
    ("ashby", r"https?://api\.ashbyhq\.com/posting-api/job-board/(?P<key>[A-Za-z0-9_.%-]+)",
     _simple("ashby", "https://jobs.ashbyhq.com/{key}")),
    ("smartrecruiters", r"https?://(?:jobs|careers)\.smartrecruiters\.com/(?P<key>(?!oneclick)[A-Za-z0-9_-]+)",
     _simple("smartrecruiters", "https://jobs.smartrecruiters.com/{key}")),
    ("smartrecruiters", r"https?://api\.smartrecruiters\.com/v1/companies/(?P<key>[A-Za-z0-9_-]+)",
     _simple("smartrecruiters", "https://jobs.smartrecruiters.com/{key}")),
    ("bamboohr", r"https?://(?P<key>[a-z0-9-]+)\.bamboohr\.com", _simple("bamboohr", "https://{key}.bamboohr.com/careers")),
    ("icims", r"https?://(?P<host>(?P<key>[a-z0-9-]+)\.icims\.com)", _simple("icims", "https://{host}/jobs/search?ss=1")),
    ("ukg", r"https?://(?P<host>recruiting2?\.ultipro\.com|[a-z0-9-]+\.rec\.pro\.ukg\.net)/(?P<key>[A-Za-z0-9]+)/"
            rf"JobBoard/(?P<site>{_GUID})", _simple("ukg", "https://{host}/{key}/JobBoard/{site}")),
    ("adp", rf"https?://workforcenow\.adp\.com/[^\s\"'<>]*?cid=(?P<key>{_GUID})", _simple("adp", None)),
    ("adp", r"https?://myjobs\.adp\.com/(?P<key>[A-Za-z0-9_-]+)", _simple("adp", "https://myjobs.adp.com/{key}")),
    ("paylocity", rf"https?://recruiting\.paylocity\.com/[Rr]ecruiting/(?:Jobs/(?:All|List|Details)|v2/api/feed/jobs)"
                  rf"/(?P<key>{_GUID})", _simple("paylocity", "https://recruiting.paylocity.com/Recruiting/Jobs/All/{key}")),
    ("paycom", r"https?://(?P<host>(?:[a-z0-9-]+\.)?paycomonline\.net)/[^\s\"'<>]*?clientkey=(?P<key>[A-Za-z0-9]+)",
     _simple("paycom", None)),
    ("dayforce", r"https?://jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<key>[A-Za-z0-9_-]+)(?:/(?P<site>[A-Za-z0-9_-]+))?",
     _simple("dayforce", None)),
    ("successfactors", r"https?://(?P<host>career\w*\.sapsf\.(?:com|eu)|[a-z0-9-]+\.successfactors\.(?:com|eu))"
                       r"/[^\s\"'<>]*?company=(?P<key>[A-Za-z0-9_]+)", _simple("successfactors", None)),
    ("taleo", r"https?://(?P<host>(?P<key>[a-z0-9-]+)\.taleo\.net)(?:/careersection/(?P<site>[A-Za-z0-9_]+))?",
     _simple("taleo", None)),
    ("jobvite", r"https?://jobs\.jobvite\.com/(?P<key>[A-Za-z0-9_-]+)", _simple("jobvite", "https://jobs.jobvite.com/{key}")),
    ("jobvite", r"https?://app\.jobvite\.com/CompanyJobs/[^\s\"'<>]*?c=(?P<key>[A-Za-z0-9]+)", _simple("jobvite", None)),
    ("workable", r"https?://apply\.workable\.com/(?P<key>(?!api\b|j\b)[A-Za-z0-9_-]+)",
     _simple("workable", "https://apply.workable.com/{key}")),
    ("workable", r"https?://(?P<key>[a-z0-9-]+)\.workable\.com", _simple("workable", "https://apply.workable.com/{key}")),
    ("jazzhr", r"https?://(?P<key>[a-z0-9-]+)\.applytojob\.com", _simple("jazzhr", "https://{key}.applytojob.com/apply")),
    ("rippling", r"https?://ats\.rippling\.com/(?P<key>(?!api\b)[A-Za-z0-9_-]+)",
     _simple("rippling", "https://ats.rippling.com/{key}/jobs")),
    ("rippling", r"https?://api\.rippling\.com/platform/api/ats/v1/board/(?P<key>[A-Za-z0-9_-]+)",
     _simple("rippling", "https://ats.rippling.com/{key}/jobs")),
    ("recruitee", r"https?://(?P<key>[a-z0-9-]+)\.recruitee\.com", _simple("recruitee", "https://{key}.recruitee.com")),
    ("breezy", r"https?://(?P<key>[a-z0-9-]+)\.breezy\.hr", _simple("breezy", "https://{key}.breezy.hr")),
]
_COMPILED = [(t, re.compile(p, re.I if t not in ("workday", "oracle", "ukg") else 0), b) for t, p, b in _MATCHERS]
_WILDCARD_TYPES = {"bamboohr", "icims", "taleo", "workable", "jazzhr", "recruitee", "breezy"}
_PHENOM_MARKERS = ("phenompeople", "cdn.phenom", "phapp.ddo", "\"phapp\"", "phenom-")
_URL_IN_TEXT = re.compile(r"https?:(?://|\\/\\/)[^\s\"'<>)\\]+(?:\\/[^\s\"'<>)\\]*)*")


def detect_from_url(url):
    """Match a single URL against the ATS patterns → result dict or None."""
    if not url:
        return None
    for ats_type, rx, build in _COMPILED:
        m = rx.search(url)
        if not m:
            continue
        key = (m.groupdict().get("key") or "").lower()
        if ats_type in _WILDCARD_TYPES and key in _GENERIC_SUBDOMAINS:
            continue
        return build(m, url)
    return None


def candidate_urls(html, base_url=None, extra=()):
    """Every URL-ish string on a page: tag attributes, inline JSON/JS, plus extras (frames, requests)."""
    urls = list(extra or [])
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(["a", "iframe", "script", "link", "form", "frame", "embed", "meta", "div", "button"]):
        for attr in ("href", "src", "action", "data-src", "data-url", "data-href", "content"):
            value = tag.get(attr)
            if value and isinstance(value, str) and not value.startswith(("#", "javascript:", "mailto:")):
                urls.append(urljoin(base_url or "", value.strip()))
    for m in _URL_IN_TEXT.finditer(html or ""):
        urls.append(m.group(0).replace("\\/", "/"))
    if base_url:
        urls.append(base_url)
    return urls


def detect_from_urls(urls):
    """Priority-ordered match over many URLs (ATS priority beats position on the page)."""
    urls = list(dict.fromkeys(u for u in urls if u))
    for ats_type, rx, build in _COMPILED:
        for url in urls:
            m = rx.search(url)
            if not m:
                continue
            key = (m.groupdict().get("key") or "").lower()
            if ats_type in _WILDCARD_TYPES and key in _GENERIC_SUBDOMAINS:
                continue
            return build(m, url)
    return None


def detect_from_html(html, base_url=None, extra_urls=()):
    """Detect the ATS behind a careers page → {ats_type, ats_host, ats_key, ats_site, careers_url} | None."""
    found = detect_from_urls(candidate_urls(html, base_url, extra_urls))
    if found:
        return found
    low = (html or "").lower()
    if any(marker in low for marker in _PHENOM_MARKERS):
        return _result("phenom", urlsplit(base_url or "").hostname, None, None, base_url)
    if extract_jsonld_jobs(html):
        return _result("jsonld", urlsplit(base_url or "").hostname, None, None, base_url)
    return None


def _jsonld_objects(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = (script.string or script.get_text() or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            try:  # some sites put raw newlines/control chars inside strings
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", text))
            except ValueError:
                continue
        pending = deque([data])  # FIFO keeps document order
        while pending:
            node = pending.popleft()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    pending.append(node["@graph"])
                if isinstance(node.get("itemListElement"), list):
                    pending.extend(i.get("item", i) if isinstance(i, dict) else i for i in node["itemListElement"])


def _has_type(node, name):
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.lower() == name.lower() for t in types)


def extract_jsonld_jobs(html):
    """All schema.org JobPosting objects on a page (handles @graph, arrays, ItemList)."""
    return [n for n in _jsonld_objects(html) if _has_type(n, "JobPosting")]


def extract_jsonld_orgs(html):
    """schema.org Organization-like objects (Organization, Corporation, LocalBusiness…)."""
    kinds = ("organization", "corporation", "localbusiness", "manufacturer")
    return [n for n in _jsonld_objects(html)
            if any(isinstance(t, str) and t.lower() in kinds
                   for t in (n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")]))]


# ── adapter registry ─────────────────────────────────────────────────────────

def _registry():
    from . import (ashby, bamboohr, breezy, greenhouse, jsonld, lever, oracle, paylocity, recruitee,
                   smartrecruiters, workable, workday)
    return {
        "workday": workday.WorkdayAdapter,
        "oracle": oracle.OracleAdapter,
        "greenhouse": greenhouse.GreenhouseAdapter,
        "lever": lever.LeverAdapter,
        "ashby": ashby.AshbyAdapter,
        "smartrecruiters": smartrecruiters.SmartRecruitersAdapter,
        "bamboohr": bamboohr.BambooHRAdapter,
        "jsonld": jsonld.JsonLdAdapter,
        "breezy": breezy.BreezyAdapter,
        "recruitee": recruitee.RecruiteeAdapter,
        "paylocity": paylocity.PaylocityAdapter,
        "workable": workable.WorkableAdapter,
    }


def has_adapter(ats_type) -> bool:
    return ats_type in _registry()


def get_adapter(ats_type, fetcher=None) -> Adapter:
    cls = _registry().get(ats_type)
    if cls is None:
        raise NotImplementedError(f"adapter pending: {ats_type}")
    return cls(fetcher)
