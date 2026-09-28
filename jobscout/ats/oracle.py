"""Oracle Recruiting Cloud (HCM Candidate Experience) adapter — verified live against
HealthPartners (fa-etnv-saasfaprod1.fa.ocs.oraclecloud.com, site "healthpartners"), 2026-09-27.

List:   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true
            &expand=requisitionList.secondaryLocations
            &finder=findReqs;siteNumber={site},keyword={kw},limit=25,offset={n},sortBy=POSTING_DATES_DESC
        → items[0].requisitionList[{Id, Title, PrimaryLocation, PostedDate, WorkplaceType(Code)}], items[0].TotalJobsCount
Detail: GET …/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}
        → items[0]{ExternalDescriptionStr, ExternalResponsibilitiesStr, ExternalQualificationsStr,
                   requisitionFlexFields[{Prompt, Value}] (pay range, work schedule…), primaryLocationCoordinates}
"""
import html as _html
import re
from urllib.parse import quote

from .base import Adapter, RawJob

PAGE = 25
MAX_PER_KEYWORD = 200
_PAY_PROMPT = re.compile(r"pay|salary|compensation|wage", re.I)


def _finder_value(text):
    return quote(str(text), safe="")


class OracleAdapter(Adapter):
    ats_type = "oracle"
    has_detail = True

    @staticmethod
    def _api(company):
        return f"https://{company['ats_host']}/hcmRestApi/resources/latest"

    @staticmethod
    def public_url(company, job_id):
        return f"https://{company['ats_host']}/hcmUI/CandidateExperience/en/sites/{company['ats_site']}/job/{job_id}"

    def list_jobs(self, company, keywords):
        jobs = {}
        for keyword in keywords or [""]:
            for req in self._search(company, keyword):
                job_id = str(req.get("Id") or "")
                if job_id and job_id not in jobs:
                    jobs[job_id] = self._to_raw(company, req)
        return list(jobs.values())

    def _search(self, company, keyword):
        offset, total = 0, None
        site = company["ats_site"]
        while True:
            finder = (f"findReqs;siteNumber={_finder_value(site)},keyword={_finder_value(keyword)},"
                      f"limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC")
            url = (f"{self._api(company)}/recruitingCEJobRequisitions?onlyData=true"
                   f"&expand=requisitionList.secondaryLocations&finder={finder}")
            data = self.fetcher.get_json(url)
            item = (data.get("items") or [{}])[0]
            if total is None:
                total = int(item.get("TotalJobsCount") or 0)
            reqs = item.get("requisitionList") or []
            yield from reqs
            offset += PAGE
            if not reqs or offset >= min(total, MAX_PER_KEYWORD):
                return

    def _to_raw(self, company, req):
        job_id = str(req["Id"])
        posted = req.get("PostedDate")
        return RawJob(
            ats_job_id=job_id,
            title=(req.get("Title") or "").strip(),
            url=self.public_url(company, job_id),
            location_text=req.get("PrimaryLocation"),
            workplace=req.get("WorkplaceTypeCode") or req.get("WorkplaceType") or None,
            posted_at=f"{posted[:10]}T00:00:00Z" if posted else None,
            extra={"secondary_locations": [s.get("Name") for s in req.get("secondaryLocations") or []
                                           if isinstance(s, dict) and s.get("Name")]},
        )

    def get_detail(self, company, raw):
        finder = f'ById;Id="{raw.ats_job_id}",siteNumber={_finder_value(company["ats_site"])}'
        url = f"{self._api(company)}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder={quote(finder, safe=';=,')}"
        data = self.fetcher.get_json(url)
        item = (data.get("items") or [None])[0]
        if not item:
            return raw
        flex = [f for f in item.get("requisitionFlexFields") or [] if f.get("Prompt") and f.get("Value")]
        sections = [item.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                          "ExternalQualificationsStr", "CorporateDescriptionStr") if item.get(k)]
        if flex:
            items = "".join(f"<li><strong>{_html.escape(f['Prompt'])}:</strong> {_html.escape(str(f['Value']))}</li>"
                            for f in flex)
            sections.append(f"<ul>{items}</ul>")
        pay = next((str(f["Value"]) for f in flex if _PAY_PROMPT.search(f["Prompt"]) and re.search(r"\d", str(f["Value"]))), None)
        schedule = " ".join(str(f["Value"]) for f in flex if re.search(r"schedule|position type|workplace", f["Prompt"], re.I))
        position_type = next((str(f["Value"]) for f in flex if re.search(r"position type", f["Prompt"], re.I)), None)
        coords = (item.get("primaryLocationCoordinates") or [{}])[0]
        raw.title = (item.get("Title") or raw.title).strip()
        raw.location_text = item.get("PrimaryLocation") or raw.location_text
        raw.workplace = item.get("WorkplaceTypeCode") or item.get("WorkplaceType") or raw.workplace
        raw.employment_type = position_type or item.get("JobSchedule") or item.get("RequisitionType")
        raw.description_html = "".join(sections)
        raw.salary_text = pay
        if item.get("ExternalPostedStartDate"):
            raw.posted_at = item["ExternalPostedStartDate"][:10] + "T00:00:00Z"
        try:
            raw.extra["lat"], raw.extra["lng"] = float(coords["Latitude"]), float(coords["Longitude"])
        except (KeyError, TypeError, ValueError):
            pass
        raw.extra["schedule_text"] = schedule
        raw.detailed = True
        return raw
