"""BambooHR careers JSON (verified live with collectivemeasures.bamboohr.com, 2026-09-27).

List:   GET https://{co}.bamboohr.com/careers/list
        → {"result":[{id, jobOpeningName, employmentStatusLabel, location{city,state}, atsLocation, isRemote,
                      locationType}]}
Detail: GET https://{co}.bamboohr.com/careers/{id}/detail
        → {"result":{"jobOpening":{jobOpeningName, description(html), compensation, datePosted, location,
                                    locationType, jobOpeningShareUrl}}}
locationType: "0" on-site, "1" remote, "2" hybrid.
"""
from .base import Adapter, RawJob

_LOCATION_TYPE = {"0": "onsite", "1": "remote", "2": "hybrid"}


def _location_text(loc, ats_loc):
    loc, ats_loc = loc or {}, ats_loc or {}
    city = loc.get("city") or ats_loc.get("city")
    state = loc.get("state") or ats_loc.get("state") or ats_loc.get("province")
    return ", ".join(x for x in (city, state) if x) or None


class BambooHRAdapter(Adapter):
    ats_type = "bamboohr"
    has_detail = True

    def list_jobs(self, company, keywords):
        data = self.fetcher.get_json(f"https://{company['ats_key']}.bamboohr.com/careers/list")
        return [self._to_raw(company, j) for j in data.get("result") or []]

    @staticmethod
    def _to_raw(company, j):
        workplace = "remote" if j.get("isRemote") else _LOCATION_TYPE.get(str(j.get("locationType")))
        return RawJob(
            ats_job_id=str(j["id"]),
            title=(j.get("jobOpeningName") or "").strip(),
            url=f"https://{company['ats_key']}.bamboohr.com/careers/{j['id']}",
            location_text=_location_text(j.get("location"), j.get("atsLocation")),
            workplace=workplace,
            employment_type=j.get("employmentStatusLabel"),
        )

    def get_detail(self, company, raw):
        data = self.fetcher.get_json(f"https://{company['ats_key']}.bamboohr.com/careers/{raw.ats_job_id}/detail")
        job = ((data.get("result") or {}).get("jobOpening") or {})
        raw.description_html = job.get("description")
        comp = job.get("compensation")
        raw.salary_text = comp if isinstance(comp, str) and comp.strip() else None
        raw.posted_at = f"{job['datePosted'][:10]}T00:00:00Z" if job.get("datePosted") else None
        raw.location_text = _location_text(job.get("location"), job.get("atsLocation")) or raw.location_text
        raw.workplace = _LOCATION_TYPE.get(str(job.get("locationType"))) or raw.workplace
        raw.url = job.get("jobOpeningShareUrl") or raw.url
        raw.detailed = True
        return raw
