"""Greenhouse job board API (public, documented; verified live with Jamf's board).

List:   GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
        → {"jobs":[{id, title, absolute_url, location{name}, first_published, updated_at, content(escaped HTML)}]}
Detail: GET …/jobs/{id}?pay_transparency=true → adds pay_input_ranges[{min_cents, max_cents, currency_type}]
"""
from ..normalize import unescape_html
from .base import Adapter, RawJob

API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter(Adapter):
    ats_type = "greenhouse"
    has_detail = True  # only for pay ranges; descriptions come with the listing

    def list_jobs(self, company, keywords):
        data = self.fetcher.get_json(f"{API}/{company['ats_key']}/jobs?content=true")
        return [self._to_raw(j) for j in data.get("jobs") or []]

    @staticmethod
    def _to_raw(job):
        return RawJob(
            ats_job_id=str(job["id"]),
            title=(job.get("title") or "").strip(),
            url=job.get("absolute_url"),
            location_text=(job.get("location") or {}).get("name"),
            posted_at=job.get("first_published") or job.get("updated_at"),
            description_html=unescape_html(job.get("content")),
            extra={"departments": [d.get("name") for d in job.get("departments") or []]},
        )

    def get_detail(self, company, raw):
        data = self.fetcher.get_json(f"{API}/{company['ats_key']}/jobs/{raw.ats_job_id}?pay_transparency=true")
        usd = [r for r in data.get("pay_input_ranges") or [] if (r.get("currency_type") or "USD") == "USD"]
        if usd:
            r = usd[0]
            raw.salary_min = (r.get("min_cents") or 0) / 100 or None
            raw.salary_max = (r.get("max_cents") or 0) / 100 or None
            raw.salary_period = "year"
        if data.get("content"):
            raw.description_html = unescape_html(data["content"])
        raw.detailed = True
        return raw
