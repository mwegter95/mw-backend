"""Workable (verified live 2026-09-27 against a Twin Cities nonprofit's board).

List:   POST https://apply.workable.com/api/v3/accounts/{account}/jobs  {"query":"", "location":[], ...}
        → {"total", "results":[{shortcode, title, remote, location{city, region, country}, published,
            type, workplace ("on_site"|"hybrid"|"remote")}], "nextPage"?}
Detail: GET  https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}
        → {..., description, requirements, benefits} (HTML)
Public job page: https://apply.workable.com/{account}/j/{shortcode}/
"""
from .base import Adapter, RawJob

API = "https://apply.workable.com/api"
_WORKPLACE = {"on_site": "onsite", "hybrid": "hybrid", "remote": "remote"}
_TYPES = {"full": "Full time", "part": "Part time", "contract": "Contract", "temporary": "Temporary"}


class WorkableAdapter(Adapter):
    ats_type = "workable"
    has_detail = True

    def list_jobs(self, company, keywords):
        account, out, token = company["ats_key"], [], None
        for _ in range(10):  # 10 pages is far beyond any local employer's board
            body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
            if token:
                body["token"] = token
            data = self.fetcher.post_json(f"{API}/v3/accounts/{account}/jobs", body)
            for job in data.get("results") or []:
                out.append(self._to_raw(account, job))
            token = data.get("nextPage")
            if not token:
                break
        return out

    @staticmethod
    def _to_raw(account, job):
        loc = job.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        workplace = "remote" if job.get("remote") else _WORKPLACE.get(job.get("workplace"))
        url = f"https://apply.workable.com/{account}/j/{job['shortcode']}/"
        return RawJob(ats_job_id=job["shortcode"], title=(job.get("title") or "").strip(), url=url,
                      apply_url=url + "apply/", location_text=where or None, city=loc.get("city"),
                      state=loc.get("region"), country=loc.get("countryCode") or loc.get("country"),
                      workplace=workplace, employment_type=_TYPES.get(job.get("type"), job.get("type")),
                      posted_at=job.get("published"))

    def get_detail(self, company, raw):
        data = self.fetcher.get_json(f"{API}/v2/accounts/{company['ats_key']}/jobs/{raw.ats_job_id}")
        parts = [data.get("description") or ""]
        for key, heading in (("requirements", "Requirements"), ("benefits", "Benefits")):
            if data.get(key):
                parts.append(f"<h3>{heading}</h3>{data[key]}")
        raw.description_html = "".join(parts)
        raw.detailed = True
        return raw
