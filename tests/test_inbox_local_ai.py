"""Inbox sorting on wegter-pc's LM Studio (inbox_local_ai.py), with LM Studio faked."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import requests
from flask import Flask

import inbox_local_ai as lai

SECRET = "test-secret-with-at-least-thirty-two-bytes"


class FakeLMStudio:
    """Answers POST /chat/completions like LM Studio would, and records what it was asked."""

    def __init__(self):
        self.requests = []
        self.mode = "ok"

    def post(self, url, json=None, timeout=None, headers=None):
        self.requests.append({"url": url, "json": json, "timeout": timeout})
        if self.mode == "down":
            raise requests.ConnectionError("refused")
        if self.mode == "slow":
            raise requests.Timeout("slow")
        schema = json["response_format"]["json_schema"]["schema"]
        if json["response_format"]["json_schema"]["name"] == "inbox_summary":
            content = '{"summary": "Mom needs an answer about Sunday."}'
        elif self.mode == "thinking":
            return _Resp({"choices": [{"message": {"content": "<think>hmm"}, "finish_reason": "length"}]})
        else:
            ids = schema["properties"]["actions"]["items"]["properties"]["id"]["enum"]
            acts = [{"id": i, "summary": f"About {i}", "labels": ["A. Read"], "importance": "high" if i == "t1" else "low",
                     "personal": i == "t1", "archive_ok": i != "t1"} for i in ids]
            acts.append({"id": "invented", "summary": "", "labels": [], "importance": "low", "personal": False,
                         "archive_ok": True})
            content = "```json\n" + __import__("json").dumps({"actions": acts, "summary": "One needs you."}) + "\n```"
        return _Resp({"model": "google/gemma-4-12b-qat",
                      "choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def client(data_dir, monkeypatch):
    (data_dir / ".secret_key").write_text(SECRET)
    users = sqlite3.connect(str(data_dir / "mw.db"))
    users.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT, display_name TEXT)")
    users.executemany("INSERT INTO users VALUES (?,?,?,?)", [(1, "zweetztuph@gmail.com", "x", "Michael"),
                                                             (2, "ashley@example.com", "x", "Ashley")])
    users.commit()
    users.close()
    monkeypatch.setenv("JOBS_ALLOWED_EMAILS", "zweetztuph@gmail.com,ashley@example.com")
    fake = FakeLMStudio()
    monkeypatch.setattr(lai.requests, "post", fake.post)
    monkeypatch.setattr(lai.jobs_ai, "health", lambda: (True, ["google/gemma-4-12b-qat"]))
    app = Flask(__name__)
    app.register_blueprint(lai.local_ai_bp)
    c = app.test_client()
    c.fake = fake
    return c


def auth(user_id=1, email="zweetztuph@gmail.com"):
    token = jwt.encode({"sub": str(user_id), "email": email, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


BODY = {"prompt": "You sort Michael's Gmail.", "emails": "---EMAIL-1---\nID: t1\n\n---EMAIL-2---\nID: t2\n",
        "ids": ["t1", "t2"]}


def test_only_the_inbox_owner_can_use_it(client):
    assert client.get("/jobs/local-ai/health").status_code == 401
    assert client.get("/jobs/local-ai/health", headers=auth(2, "ashley@example.com")).status_code == 403
    health = client.get("/jobs/local-ai/health", headers=auth()).get_json()
    assert health["ok"] is True and health["model_ready"] is True


def test_sort_returns_one_clean_action_per_email(client):
    out = client.post("/jobs/local-ai/inbox/sort", json=BODY, headers=auth()).get_json()
    assert [a["id"] for a in out["actions"]] == ["t1", "t2"], "invented ids are dropped"
    assert out["actions"][0] == {"id": "t1", "summary": "About t1", "labels": ["A. Read"], "importance": "high",
                                 "personal": True, "archive_ok": False}
    assert out["summary"] == "One needs you." and out["missing"] == []
    sent = client.fake.requests[0]["json"]
    schema = sent["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["actions"]["minItems"] == 2
    assert schema["properties"]["actions"]["items"]["properties"]["id"]["enum"] == ["t1", "t2"]
    assert sent["messages"][0]["content"] == BODY["prompt"]
    assert client.fake.requests[0]["timeout"] < 100, "must finish inside Cloudflare's 100 s"


def test_limits_on_what_one_call_may_carry(client):
    too_many = {**BODY, "ids": [f"t{i}" for i in range(20)]}
    assert client.post("/jobs/local-ai/inbox/sort", json=too_many, headers=auth()).status_code == 413
    assert client.post("/jobs/local-ai/inbox/sort", json={"prompt": "x"}, headers=auth()).status_code == 400


@pytest.mark.parametrize("mode,code,words", [
    ("down", "lmstudio_down", "LM Studio isn't answering"),
    ("slow", "timeout", "took too long"),
    ("thinking", "cut_off", "Reasoning"),
])
def test_failures_say_what_to_do_and_never_502(client, mode, code, words):
    client.fake.mode = mode
    r = client.post("/jobs/local-ai/inbox/sort", json=BODY, headers=auth())
    assert r.status_code == 424
    assert r.get_json()["code"] == code and words in r.get_json()["error"]


def test_one_call_at_a_time(client):
    lai._busy.acquire()
    try:
        r = client.post("/jobs/local-ai/inbox/sort", json=BODY, headers=auth())
        assert r.status_code == 409 and r.get_json()["code"] == "busy"
    finally:
        lai._busy.release()


def test_summarize_writes_the_batch_note(client):
    out = client.post("/jobs/local-ai/inbox/summarize", json={"lines": ["Mom: Sunday dinner (high)"]},
                      headers=auth()).get_json()
    assert out == {"summary": "Mom needs an answer about Sunday."}
