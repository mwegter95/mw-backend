import datetime
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

try:
    import google_auth_oauthlib.flow  # noqa: F401
    import google.oauth2.credentials  # noqa: F401
    import googleapiclient.discovery  # noqa: F401
except ModuleNotFoundError:
    google_auth_oauthlib = types.ModuleType("google_auth_oauthlib")
    google_auth_oauthlib_flow = types.ModuleType("google_auth_oauthlib.flow")
    google = types.ModuleType("google")
    google_oauth2 = types.ModuleType("google.oauth2")
    google_oauth2_credentials = types.ModuleType("google.oauth2.credentials")
    googleapiclient = types.ModuleType("googleapiclient")
    googleapiclient_discovery = types.ModuleType("googleapiclient.discovery")

    class _DummyFlow:
        @classmethod
        def from_client_config(cls, *_args, **_kwargs):
            return cls()

    class _DummyCredentials:
        def __init__(self, *_args, **_kwargs):
            pass

    google_auth_oauthlib_flow.Flow = _DummyFlow
    google_oauth2_credentials.Credentials = _DummyCredentials
    googleapiclient_discovery.build = lambda *_args, **_kwargs: None

    google_auth_oauthlib.flow = google_auth_oauthlib_flow
    google.oauth2 = google_oauth2
    google_oauth2.credentials = google_oauth2_credentials
    googleapiclient.discovery = googleapiclient_discovery

    sys.modules["google_auth_oauthlib"] = google_auth_oauthlib
    sys.modules["google_auth_oauthlib.flow"] = google_auth_oauthlib_flow
    sys.modules["google"] = google
    sys.modules["google.oauth2"] = google_oauth2
    sys.modules["google.oauth2.credentials"] = google_oauth2_credentials
    sys.modules["googleapiclient"] = googleapiclient
    sys.modules["googleapiclient.discovery"] = googleapiclient_discovery

import life_gcal


class _Call:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class _CalendarList:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def list(self):
        return _Call(self.result, self.error)


class _Events:
    def __init__(self, responses):
        self.responses = responses

    def list(self, calendarId, **_kwargs):
        result = self.responses[calendarId]
        if isinstance(result, Exception):
            return _Call(error=result)
        return _Call(result=result)


class _CalendarService:
    def __init__(self, calendar_result=None, calendar_error=None, event_responses=None):
        self.calendar_result = calendar_result
        self.calendar_error = calendar_error
        self.event_responses = event_responses or {}

    def calendarList(self):
        return _CalendarList(self.calendar_result, self.calendar_error)

    def events(self):
        return _Events(self.event_responses)


class LifeGcalTests(unittest.TestCase):
    def test_calendar_list_failure_is_not_reported_as_empty_events(self):
        service = _CalendarService(calendar_error=RuntimeError("token expired"))

        with patch.object(life_gcal, "build", return_value=service):
            with self.assertRaises(life_gcal.CalendarFetchError):
                life_gcal.list_upcoming_events("refresh")

    def test_all_event_fetch_failures_are_not_reported_as_empty_events(self):
        service = _CalendarService(
            calendar_result={"items": [{"id": "primary"}]},
            event_responses={"primary": RuntimeError("unauthorized")},
        )

        with patch.object(life_gcal, "build", return_value=service):
            with self.assertRaises(life_gcal.CalendarFetchError):
                life_gcal.list_upcoming_events("refresh")

    def test_partial_calendar_failure_keeps_successful_events(self):
        service = _CalendarService(
            calendar_result={"items": [{"id": "primary"}, {"id": "shared"}]},
            event_responses={
                "primary": {
                    "items": [{
                        "id": "event-1",
                        "summary": "Dentist",
                        "start": {"date": "2026-07-02"},
                        "end": {"date": "2026-07-03"},
                    }],
                },
                "shared": RuntimeError("calendar disabled"),
            },
        )

        with patch.object(life_gcal, "build", return_value=service):
            events = life_gcal.list_upcoming_events("refresh")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Dentist")
        self.assertEqual(events[0]["calendarId"], "primary")


class SmartGenerationTests(unittest.TestCase):
    def test_zero_events_leave_existing_smart_reminders_unchanged(self):
        try:
            import server
        except ModuleNotFoundError as exc:
            self.skipTest(f"server dependencies are not installed: {exc.name}")

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE life_gcal_accounts (
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                refresh_token_enc TEXT NOT NULL,
                last_synced_at DATETIME,
                last_generated_at DATETIME,
                PRIMARY KEY (owner_type, owner_id)
            );
            CREATE TABLE life_habits (
                id TEXT NOT NULL,
                owner_type TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at DATETIME,
                PRIMARY KEY (id, owner_type, owner_id)
            );
        """)
        db.execute(
            "INSERT INTO life_gcal_accounts (owner_type, owner_id, refresh_token_enc) VALUES (?, ?, ?)",
            ("user", "1", "encrypted"),
        )
        db.execute(
            "INSERT INTO life_habits (id, owner_type, owner_id, data, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("gcal-old", "user", "1", '{"source":"gcal-ai","freq":{"kind":"date","date":"2026-07-01"}}', "2026-06-01"),
        )
        db.commit()

        with patch.object(server, "_GCAL_AVAILABLE", True), \
             patch.object(server.life_gcal, "is_configured", return_value=True), \
             patch.object(server, "_gcal_decrypt", return_value="refresh"), \
             patch.object(server.life_gcal, "list_upcoming_events", return_value=[]), \
             patch.object(server, "utc_now", return_value=datetime.datetime(2026, 6, 11, tzinfo=datetime.UTC)), \
             patch.object(server, "utc_now_iso_legacy", return_value="2026-06-11T12:00:00"):
            result = server._smart_generate_for_owner(db, "user", "1")

        self.assertTrue(result["skipped"])
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["pruned"], 0)
        remaining = db.execute("SELECT COUNT(*) FROM life_habits WHERE id='gcal-old'").fetchone()[0]
        self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()
