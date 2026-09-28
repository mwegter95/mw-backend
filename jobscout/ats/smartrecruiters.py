"""SmartRecruiters public postings API (verified live, 2026-09-27).

List:   GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N[&q=keyword]
        → {totalFound, content:[{id, name, releasedDate, location{city, region, country, remote, hybrid,
                                 fullLocation}, typeOfEmployment{label}}]}
Detail: GET …/postings/{id} → {postingUrl, applyUrl, jobAd{sections{companyDescription, jobDescription,
                                qualifications, additionalInformation}}}
Small boards are read whole; boards over 300 postings are searched per keyword.
"""
from urllib.parse import quote

from .base import Adapter, RawJob

API = "https://api.smartrecruiters.com/v1/companies"
PAGE = 100
WHOLE_BOARD_MAX = 300


class SmartRecruitersAdapter(Adapter):
    ats_type = "smartrecruiters"
    has_detail = True

    def list_jobs(self, company, keywords):
        first = self._page(company, None, 0)
        queries = [None] if int(first.get("totalFound") or 0) <= WHOLE_BOARD_MAX else list(keywords or [None])
        jobs = {}
        for q in queries:
            offset, data = 0, first if q is None else self._page(company, q, 0)
            while True:
                for p in data.get("content") or []:
                    jobs.setdefault(str(p["id"]), self._to_raw(company, p))
                offset += PAGE
                if offset >= min(int(data.get("totalFound") or 0), WHOLE_BOARD_MAX) or not data.get("content"):
                    break
                data = self._page(company, q, offset)
        return list(jobs.values())

    def _page(self, company, q, offset):
        url = f"{API}/{company['ats_key']}/postings?limit={PAGE}&offset={offset}"
        if q:
            url += f"&q={quote(q)}"
        return self.fetcher.get_json(url)

    @staticmethod
    def _to_raw(company, p):
        loc = p.get("location") or {}
        city, region = loc.get("city"), loc.get("region")
        text = loc.get("fullLocation") or ", ".join(x for x in (city, region, (loc.get("country") or "").upper()) if x)
        workplace = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else None
        return RawJob(
            ats_job_id=str(p["id"]),
            title=(p.get("name") or "").strip(),
            url=f"https://jobs.smartrecruiters.com/{company['ats_key']}/{p['id']}",
            location_text=text,
            country=(loc.get("country") or "").upper() or None,
            workplace=workplace,
            employment_type=(p.get("typeOfEmployment") or {}).get("label"),
            posted_at=p.get("releasedDate"),
        )

    def get_detail(self, company, raw):
        data = self.fetcher.get_json(f"{API}/{company['ats_key']}/postings/{raw.ats_job_id}")
        sections = ((data.get("jobAd") or {}).get("sections") or {})
        parts = []
        for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
            sec = sections.get(key) or {}
            if sec.get("text"):
                parts.append(f"<h3>{sec.get('title') or ''}</h3>{sec['text']}")
        raw.description_html = "".join(parts)
        raw.url = data.get("postingUrl") or raw.url
        raw.apply_url = data.get("applyUrl")
        raw.detailed = True
        return raw
