"""Paylocity recruiting (verified live 2026-09-27 against a Twin Cities HVAC distributor's board).

The public board page https://recruiting.paylocity.com/Recruiting/Jobs/All/{companyGuid} embeds
`window.pageData = {..., "Jobs": [{JobId, JobTitle, LocationName, PublishedDate, Description, ...}]}`.
(The v2 JSON feed at /recruiting/v2/api/feed/jobs/{guid} is only filled for job-board syndication and
is usually empty.) Each job's page /Recruiting/Jobs/Details/{JobId} carries schema.org JobPosting
JSON-LD with the full description, location and, when posted, the pay range.
"""
import json
import re

from . import extract_jsonld_jobs
from .base import Adapter, RawJob
from .jsonld import jobposting_to_raw

BOARD = "https://recruiting.paylocity.com/Recruiting/Jobs/All/{key}"
DETAIL = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}"
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*")


def page_data(html):
    """The JSON object assigned to window.pageData (decoded without trusting what follows it)."""
    m = _PAGE_DATA.search(html or "")
    if not m:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


class PaylocityAdapter(Adapter):
    ats_type = "paylocity"
    has_detail = True

    def list_jobs(self, company, keywords):
        html, _ = self.fetcher.get_page(BOARD.format(key=company["ats_key"]))
        out = []
        for job in page_data(html).get("Jobs") or []:
            if not job.get("JobId") or not job.get("JobTitle"):
                continue
            url = DETAIL.format(job_id=job["JobId"])
            location = job.get("LocationName")
            out.append(RawJob(
                ats_job_id=str(job["JobId"]),
                title=job["JobTitle"].strip(),
                url=url,
                apply_url=url.replace("/Details/", "/Apply/"),
                # "Headquarters" and similar internal site names aren't places; leave those to the detail page
                location_text=location if location and "," in location else None,
                posted_at=job.get("PublishedDate"),
                description_html=job.get("Description"),
                extra={"location_name": location},
            ))
        return out

    def get_detail(self, company, raw):
        html, final = self.fetcher.get_page(raw.url)
        postings = extract_jsonld_jobs(html)
        if postings:
            detail = jobposting_to_raw(postings[0], final)
            for field in ("location_text", "city", "state", "country", "workplace", "employment_type",
                          "salary_text", "salary_min", "salary_max", "salary_period"):
                if getattr(detail, field, None) and not getattr(raw, field, None):
                    setattr(raw, field, getattr(detail, field))
            raw.description_html = detail.description_html or raw.description_html
            raw.posted_at = raw.posted_at or detail.posted_at
        raw.detailed = True
        return raw
