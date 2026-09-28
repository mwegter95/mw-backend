"""Shared pytest fixtures for Job Scout: a temp data dir per test and an offline fake fetcher."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name):
    return json.loads(fixture_text(name))


class FakeResponse:
    def __init__(self, payload, url):
        self._payload, self.url = payload, url
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return json.loads(self.text) if isinstance(self._payload, str) else self._payload


class FakeFetcher:
    """Routes URLs to canned responses: routes = [(substring, response | callable(url, payload))].
    A value that is an Exception instance is raised. Unmatched URLs raise FetchError(404)."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []

    def _resolve(self, url, payload=None):
        from jobscout.http import FetchError
        self.calls.append((url, payload))
        for needle, value in self.routes:
            if needle in url:
                value = value(url, payload) if callable(value) else value
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchError(url, "HTTP 404", 404)

    def get_page(self, url, **_):
        value = self._resolve(url)
        return (value, url) if isinstance(value, str) else (json.dumps(value), url)

    def get_json(self, url, **_):
        value = self._resolve(url)
        return json.loads(value) if isinstance(value, str) else value

    def post_json(self, url, payload, **_):
        value = self._resolve(url, payload)
        return json.loads(value) if isinstance(value, str) else value

    def request(self, method, url, **kwargs):
        return FakeResponse(self._resolve(url, kwargs.get("json") or kwargs.get("data")), url)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Fresh JOBSCOUT_DATA_DIR with an initialised jobscout.db."""
    monkeypatch.setenv("JOBSCOUT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    for var in ("JOBS_SCHEDULER", "JOBS_AI_LOCAL", "MW_ROLE", "JOBS_ALLOWED_EMAILS"):
        monkeypatch.delenv(var, raising=False)  # a developer shell must not change test results
    from jobscout import db
    db.init()
    return tmp_path


@pytest.fixture
def conn(data_dir):
    from jobscout import db
    c = db.connect()
    yield c
    c.close()


def insert_company(conn, **fields):
    from jobscout import db
    now = db.now_iso()
    cols = {"name": "Acme Plastics", "domain": "acmeplastics.com", "homepage_url": "https://acmeplastics.com",
            "status": "active", "enrich_status": "done", "industry": "mfg_plastics_packaging",
            "employee_band": "200-999", "hq_city": "Oakdale", "hq_state": "MN", "lat": 44.963, "lng": -92.965,
            "geo_precision": "city", "entity_type": "company", "local_presence": "hq", "created_at": now,
            "updated_at": now, **fields}
    cur = conn.execute(f"INSERT INTO companies({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       tuple(cols.values()))
    conn.commit()
    return cur.lastrowid


def insert_job(conn, company_id, **fields):
    from jobscout import db
    now = db.now_iso()
    cols = {"company_id": company_id, "ats_job_id": fields.pop("ats_job_id", f"job-{now}-{len(fields)}"),
            "source": "ats", "title": "Marketing Manager", "title_tier": "manager", "prefilter": "pass",
            "rule_score": 72, "location_text": "Oakdale, MN", "city": "Oakdale", "state": "MN", "country": "US",
            "lat": 44.963, "lng": -92.965, "geo_precision": "city", "workplace": "hybrid",
            "salary_min": 95000, "salary_max": 120000, "salary_period": "year", "salary_text": "$95,000 - $120,000",
            "description_html": "<p>Lead marketing.</p>", "description_text": "Lead marketing.",
            "posted_at": now, "first_seen_at": now, "last_seen_at": now, "content_hash": "abc", **fields}
    cur = conn.execute(f"INSERT INTO jobs({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(cols.values()))
    conn.commit()
    return cur.lastrowid
