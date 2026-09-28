"""Recruitee careers-site API.

GET https://{co}.recruitee.com/api/offers/  → {"offers":[{id, slug, title, description, requirements, location,
    city, state_code, country_code, remote, hybrid, on_site, employment_type_code, careers_url,
    careers_apply_url, published_at, salary{min, max, period, currency}}]}
The endpoint shape was verified live (2026-09-27) but only against an empty public board; field names follow
Recruitee's Careers Site API documentation.
"""
from .base import Adapter, RawJob


class RecruiteeAdapter(Adapter):
    ats_type = "recruitee"

    def list_jobs(self, company, keywords):
        data = self.fetcher.get_json(f"https://{company['ats_key']}.recruitee.com/api/offers/")
        return [self._to_raw(o) for o in data.get("offers") or []]

    @staticmethod
    def _to_raw(o):
        salary = o.get("salary") or {}
        usd = (salary.get("currency") or "USD") == "USD"
        workplace = "remote" if o.get("remote") else "hybrid" if o.get("hybrid") else "onsite" if o.get("on_site") else None
        country = (o.get("country_code") or "").upper() or None
        return RawJob(
            ats_job_id=str(o["id"]),
            title=(o.get("title") or "").strip(),
            url=o.get("careers_url"),
            apply_url=o.get("careers_apply_url"),
            location_text=o.get("location"),
            city=o.get("city"), state=o.get("state_code") if country in (None, "US") else None, country=country,
            workplace=workplace,
            employment_type=o.get("employment_type_code"),
            posted_at=o.get("published_at"),
            description_html=(o.get("description") or "") + (o.get("requirements") or ""),
            salary_min=salary.get("min") if usd else None,
            salary_max=salary.get("max") if usd else None,
            salary_period=salary.get("period"),
            detailed=True,
        )
