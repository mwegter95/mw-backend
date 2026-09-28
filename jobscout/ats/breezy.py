"""Breezy HR (verified live with breezy.breezy.hr, 2026-09-27).

List:   GET https://{co}.breezy.hr/json
        → [{id, friendly_id, name, url, published_date, type{name}, location{city, state{id}, country{id},
            is_remote, name}, salary (text), department}]
Detail: the posting page https://{co}.breezy.hr/p/{friendly_id} — description in div.description.
"""
from bs4 import BeautifulSoup

from .base import Adapter, RawJob


class BreezyAdapter(Adapter):
    ats_type = "breezy"
    has_detail = True

    def list_jobs(self, company, keywords):
        data = self.fetcher.get_json(f"https://{company['ats_key']}.breezy.hr/json")
        return [self._to_raw(company, p) for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    @staticmethod
    def _to_raw(company, p):
        loc = p.get("location") or {}
        state = (loc.get("state") or {}).get("id")
        country = (loc.get("country") or {}).get("id")
        return RawJob(
            ats_job_id=str(p["id"]),
            title=(p.get("name") or "").strip(),
            url=p.get("url") or f"https://{company['ats_key']}.breezy.hr/p/{p.get('friendly_id')}",
            location_text=loc.get("name"),
            city=loc.get("city"), state=state if country in (None, "US") else None, country=country,
            workplace="remote" if loc.get("is_remote") else None,
            employment_type=(p.get("type") or {}).get("name"),
            posted_at=p.get("published_date"),
            salary_text=p.get("salary") or None,
        )

    def get_detail(self, company, raw):
        html, _ = self.fetcher.get_page(raw.url)
        div = BeautifulSoup(html, "html.parser").select_one("div.description")
        raw.description_html = str(div) if div else None
        raw.detailed = True
        return raw
