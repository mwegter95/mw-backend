"""Workday adapter (verified live against 3M, 2026-09-27).

List:   POST https://{host}/wday/cxs/{tenant}/{site}/jobs
        {"appliedFacets":{}, "limit":20, "offset":N, "searchText":"marketing"}
        → {"total", "jobPostings":[{title, externalPath, locationsText, postedOn, remoteType, bulletFields}], "facets"}
Detail: GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}
        → {"jobPostingInfo": {title, location, additionalLocations, remoteType, timeType, startDate,
                              jobReqId, externalUrl, jobDescription}}

Big tenants are searched per function keyword and, when the first page offers a country facet,
re-queried restricted to the United States so paging stays small.
"""
import re
from datetime import datetime, timedelta, timezone

from ..normalize import is_local, parse_location, state_code
from .base import Adapter, RawJob

PAGE = 20
MAX_PER_KEYWORD = 200
_US_DESCRIPTORS = {"united states of america", "united states", "usa", "us"}
_POSTED_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def posted_on_to_iso(text, today=None):
    """"Posted 3 Days Ago" / "Posted Today" / "Posted 30+ Days Ago" → approximate ISO date."""
    m = _POSTED_RE.search(text or "")
    if not m:
        return None
    today = today or datetime.now(timezone.utc).date()
    word = m.group(1).lower()
    days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))
    return (today - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")


def location_from_path(external_path):
    """"/job/US-Minnesota-Maplewood/Title_R123" → ("Maplewood", "MN", "US") when parseable."""
    parts = (external_path or "").strip("/").split("/")
    if len(parts) < 3 or parts[0] != "job":
        return None
    tokens = parts[1].split("-")
    if len(tokens) < 2 or not re.fullmatch(r"[A-Z]{2}", tokens[0]):
        return None
    country, rest = tokens[0], tokens[1:]
    for n in (3, 2, 1):  # longest state name first ("North-Dakota", "New-York")
        st = state_code(" ".join(rest[:n])) if len(rest) >= n else None
        if st:
            city = " ".join(rest[n:]) or None
            return city, st, country
    return None, None, country


def _find_us_facet(facets, parent=None):
    """(facetParameter, value id, count) for a "United States" facet value, searching nested groups."""
    for facet in facets or []:
        param = facet.get("facetParameter") or parent
        for value in facet.get("values") or []:
            if value.get("values"):
                found = _find_us_facet([value], param)
                if found:
                    return found
            elif (value.get("descriptor") or "").strip().lower() in _US_DESCRIPTORS and value.get("id"):
                return param, value["id"], int(value.get("count") or 0)
    return None


class WorkdayAdapter(Adapter):
    ats_type = "workday"
    has_detail = True

    @staticmethod
    def api_base(company):
        return f"https://{company['ats_host']}/wday/cxs/{company['ats_key']}/{company['ats_site']}"

    @staticmethod
    def public_base(company):
        host, tenant, site = company["ats_host"], company["ats_key"], company["ats_site"]
        if "myworkdaysite" in host:
            return f"https://{host}/recruiting/{tenant}/{site}"
        return f"https://{host}/{site}"

    def list_jobs(self, company, keywords):
        base = self.api_base(company)
        jobs = {}
        for keyword in keywords or [""]:
            for posting in self._search(base, keyword):
                path = posting.get("externalPath")
                if path and path not in jobs:
                    jobs[path] = self._to_raw(company, posting)
        return list(jobs.values())

    def _search(self, base, keyword):
        facets, offset, total = {}, 0, None
        while True:
            body = {"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": keyword}
            data = self.fetcher.post_json(base + "/jobs", body)
            if total is None:
                total = int(data.get("total") or 0)
                us = _find_us_facet(data.get("facets")) if total > PAGE else None
                if us and us[2] < total:
                    facets, total = {us[0]: [us[1]]}, us[2]
                    continue  # restart page 0 with the US-only facet
            postings = data.get("jobPostings") or []
            yield from postings
            offset += PAGE
            if not postings or offset >= min(total, MAX_PER_KEYWORD):
                return

    def _to_raw(self, company, posting):
        path = posting["externalPath"]
        raw = RawJob(
            ats_job_id=path,
            title=(posting.get("title") or "").strip(),
            url=self.public_base(company) + path,
            location_text=posting.get("locationsText"),
            workplace=posting.get("remoteType"),
            posted_at=posted_on_to_iso(posting.get("postedOn")),
            extra={"path": path, "req_id": (posting.get("bulletFields") or [None])[0]},
        )
        if not parse_location(raw.location_text).state:  # "3 Locations" → use the path's primary location
            hint = location_from_path(path)
            if hint:
                raw.city, raw.state, raw.country = hint
        return raw

    def get_detail(self, company, raw):
        data = self.fetcher.get_json(self.api_base(company) + raw.extra["path"])
        info = data.get("jobPostingInfo") or {}
        locations = [x for x in [info.get("location")] + list(info.get("additionalLocations") or []) if x]
        chosen = next((x for x in locations if parse_location(x).state and is_local(parse_location(x))), None)
        chosen = chosen or (locations[0] if locations else raw.location_text)
        loc = parse_location(chosen)
        raw.title = (info.get("title") or raw.title).strip()
        raw.location_text = chosen
        raw.city, raw.state, raw.country = loc.city, loc.state, loc.country
        raw.workplace = info.get("remoteType") or raw.workplace
        raw.employment_type = info.get("timeType")
        if info.get("startDate"):
            raw.posted_at = f"{info['startDate'][:10]}T00:00:00Z"
        raw.url = info.get("externalUrl") or raw.url
        raw.description_html = info.get("jobDescription")
        raw.extra.update(req_id=info.get("jobReqId") or raw.extra.get("req_id"), locations=locations)
        raw.detailed = True
        return raw
