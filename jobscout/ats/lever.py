"""Lever postings API (public, documented; verified live with Spotify's board).

GET https://api.lever.co/v0/postings/{company}?mode=json   (EU: api.eu.lever.co)
→ [{id, text, hostedUrl, applyUrl, createdAt(ms), categories{location, commitment, allLocations},
    workplaceType, description, lists[{text, content}], additional, salaryRange{min,max,currency,interval}}]
"""
from datetime import datetime, timezone

from .base import Adapter, RawJob


class LeverAdapter(Adapter):
    ats_type = "lever"

    @staticmethod
    def _api(company):
        eu = "eu." if "eu." in (company.get("ats_host") or "") else ""
        return f"https://api.{eu}lever.co/v0/postings/{company['ats_key']}?mode=json"

    def list_jobs(self, company, keywords):
        data = self.fetcher.get_json(self._api(company))
        return [self._to_raw(p) for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    @staticmethod
    def _to_raw(p):
        cats = p.get("categories") or {}
        parts = [p.get("description") or ""]
        for block in p.get("lists") or []:
            parts.append(f"<h3>{block.get('text') or ''}</h3><ul>{block.get('content') or ''}</ul>")
        parts.append(p.get("additional") or "")
        salary = p.get("salaryRange") or {}
        created = p.get("createdAt")
        posted = (datetime.fromtimestamp(created / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                  if isinstance(created, (int, float)) else None)
        usd = (salary.get("currency") or "USD") == "USD"
        return RawJob(
            ats_job_id=p["id"],
            title=(p.get("text") or "").strip(),
            url=p.get("hostedUrl"),
            apply_url=p.get("applyUrl"),
            location_text="; ".join(cats.get("allLocations") or []) or cats.get("location"),
            country=p.get("country"),
            workplace=p.get("workplaceType"),
            employment_type=cats.get("commitment"),
            posted_at=posted,
            description_html="".join(parts),
            salary_text=p.get("salaryDescriptionPlain") or None,
            salary_min=salary.get("min") if usd else None,
            salary_max=salary.get("max") if usd else None,
            salary_period=salary.get("interval"),
            detailed=True,
        )
