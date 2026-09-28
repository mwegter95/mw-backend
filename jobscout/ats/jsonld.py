"""Generic schema.org JobPosting adapter.

list_jobs reads the careers page: JobPosting JSON-LD found there becomes complete RawJobs; otherwise
job-looking links on the same site become title-only RawJobs whose detail page is parsed for JSON-LD
by get_detail (the sweep only fetches details for titles that pass the prefilter).
"""
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import browser
from ..normalize import content_hash, parse_location, unescape_html
from . import extract_jsonld_jobs
from .base import Adapter, RawJob

_JOB_PATH = re.compile(r"/(jobs?|careers?|positions?|openings?|opportunit(y|ies)|postings?|vacanc(y|ies)|"
                       r"requisitions?)/[^/?#]{3,}", re.I)
_NAV_TEXT = re.compile(r"^(careers?|jobs?|search( all)? jobs|view (all|jobs|openings)|apply( now)?|benefits|"
                       r"our culture|life at .*|join (us|our team)|open positions|current openings|learn more|"
                       r"read more|home|about( us)?|contact( us)?)$", re.I)
MAX_LINKS = 80


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _text(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("value")
    return value


def _place_parts(place):
    place = place or {}
    addr = place.get("address") or {}
    if isinstance(addr, str):
        return addr, None, None
    country = _text(addr.get("addressCountry"))
    text = ", ".join(str(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"), country) if x)
    geo = place.get("geo") or {}
    try:
        coords = (float(geo["latitude"]), float(geo["longitude"]))
    except (KeyError, TypeError, ValueError):
        coords = None
    return text or None, coords, addr.get("postalCode")


def jobposting_to_raw(node, page_url):
    """Map one schema.org JobPosting object to a RawJob."""
    places = node.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    parsed = [_place_parts(p) for p in places if isinstance(p, dict)]
    local = next((p for p in parsed if p[0] and parse_location(p[0]).state in ("MN", "WI")), None)
    text, coords, _ = local or (parsed[0] if parsed else (None, None, None))
    remote = "TELECOMMUTE" in str(node.get("jobLocationType") or "").upper()
    if remote and not text:
        req = _first(node.get("applicantLocationRequirements")) or {}
        text = f"Remote - {_text(req) or 'US'}"
    salary = node.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else None
    if isinstance(value, (int, float)):
        value = {"value": value}
    value = value if isinstance(value, dict) else {}
    usd = (salary.get("currency") or "USD") == "USD" if isinstance(salary, dict) else True
    ident = node.get("identifier")
    ident = _text(ident) if isinstance(ident, dict) else ident
    title = (node.get("title") or node.get("name") or "").strip()
    url = node.get("url") or page_url
    description = node.get("description") or ""
    if "&lt;" in description:
        description = unescape_html(description)
    employment = node.get("employmentType")
    raw = RawJob(
        ats_job_id=str(ident or url if (ident or url != page_url) else content_hash(title, text)),
        title=title,
        url=urljoin(page_url, url),
        location_text=text,
        workplace="remote" if remote else None,
        employment_type=", ".join(employment) if isinstance(employment, list) else employment,
        posted_at=node.get("datePosted"),
        description_html=description,
        salary_min=(value.get("minValue") or value.get("value")) if usd else None,
        salary_max=(value.get("maxValue") or value.get("value")) if usd else None,
        salary_period=value.get("unitText"),
        detailed=True,
    )
    if coords:
        raw.extra["lat"], raw.extra["lng"] = coords
    return raw


def job_links(html, page_url):
    """Same-site links that look like individual job postings → [(url, anchor text)]."""
    soup = BeautifulSoup(html or "", "html.parser")
    site = (urlsplit(page_url).hostname or "").removeprefix("www.")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0]
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if host != site or url in seen or not _JOB_PATH.search(urlsplit(url).path):
            continue
        if not (4 <= len(text) <= 120) or _NAV_TEXT.match(text) or not re.search(r"[A-Za-z]{3}", text):
            continue
        seen.add(url)
        out.append((url, text))
    return out[:MAX_LINKS]


class JsonLdAdapter(Adapter):
    ats_type = "jsonld"
    has_detail = True

    def list_jobs(self, company, keywords):
        page_url = company.get("careers_url") or company.get("homepage_url")
        html, final_url, _ = browser.page_html(page_url, self.fetcher)
        postings = extract_jsonld_jobs(html)
        if postings:
            return [jobposting_to_raw(p, final_url) for p in postings]
        return [RawJob(ats_job_id=url, title=text, url=url, extra={"link": True}) for url, text in job_links(html, final_url)]

    def get_detail(self, company, raw):
        if not raw.extra.get("link"):
            return raw
        html, final_url, _ = browser.page_html(raw.url, self.fetcher)
        postings = extract_jsonld_jobs(html)
        if not postings:
            return raw
        detailed = jobposting_to_raw(postings[0], final_url)
        detailed.ats_job_id, detailed.url = raw.ats_job_id, raw.url
        return detailed
