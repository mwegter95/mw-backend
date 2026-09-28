"""Common types for ATS adapters."""
from dataclasses import dataclass, field

from ..http import get_fetcher


@dataclass
class RawJob:
    """A job as read from an ATS, before normalisation. Only ats_job_id and title are required."""
    ats_job_id: str
    title: str
    url: str = None
    apply_url: str = None
    location_text: str = None
    city: str = None
    state: str = None
    country: str = None
    workplace: str = None          # raw ATS value or enum; normalised later
    employment_type: str = None
    posted_at: str = None          # ISO date/time
    description_html: str = None
    salary_text: str = None
    salary_min: float = None
    salary_max: float = None
    salary_period: str = None      # raw period ("year", "hour", "1 YEAR", …)
    extra: dict = field(default_factory=dict)   # adapter-private data (detail refs, lat/lng, …)
    detailed: bool = False         # True once description/detail fields are filled


class Adapter:
    """Base adapter. Subclasses implement list_jobs and, when the listing lacks descriptions,
    get_detail (setting has_detail = True)."""
    ats_type = ""
    has_detail = False

    def __init__(self, fetcher=None):
        self.fetcher = fetcher or get_fetcher()

    def list_jobs(self, company: dict, keywords: list) -> list:
        raise NotImplementedError(f"adapter pending: {self.ats_type}")

    def get_detail(self, company: dict, raw: RawJob) -> RawJob:
        return raw

