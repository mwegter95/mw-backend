"""Ashby posting API (public, documented; verified live with Ramp's board).

GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
→ {"jobs":[{id, title, location, secondaryLocations, workplaceType, isRemote, employmentType, publishedAt,
            descriptionHtml, jobUrl, applyUrl, address{postalAddress}, compensation{summaryComponents,
            scrapeableCompensationSalarySummary}}]}
"""
from .base import Adapter, RawJob


def _salary_component(comp):
    for c in (comp or {}).get("summaryComponents") or []:
        if c.get("compensationType") == "Salary" and (c.get("currencyCode") or "USD") == "USD":
            return c
    return None


class AshbyAdapter(Adapter):
    ats_type = "ashby"

    def list_jobs(self, company, keywords):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company['ats_key']}?includeCompensation=true"
        data = self.fetcher.get_json(url)
        return [self._to_raw(j) for j in data.get("jobs") or [] if j.get("isListed", True)]

    @staticmethod
    def _to_raw(j):
        locations = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        comp = j.get("compensation") or {}
        salary = _salary_component(comp)
        postal = ((j.get("address") or {}).get("postalAddress") or {})
        return RawJob(
            ats_job_id=j["id"],
            title=(j.get("title") or "").strip(),
            url=j.get("jobUrl"),
            apply_url=j.get("applyUrl"),
            location_text="; ".join(x for x in locations if x),
            workplace=j.get("workplaceType") or ("remote" if j.get("isRemote") else None),
            employment_type=j.get("employmentType"),
            posted_at=j.get("publishedAt"),
            description_html=j.get("descriptionHtml"),
            salary_text=comp.get("scrapeableCompensationSalarySummary"),
            salary_min=salary.get("minValue") if salary else None,
            salary_max=salary.get("maxValue") if salary else None,
            salary_period=salary.get("interval") if salary else None,
            extra={"postal": postal},
            detailed=True,
        )
