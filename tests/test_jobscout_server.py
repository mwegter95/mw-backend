"""The Job Scout server on wegter-pc: sign-in checked against the Surface, the in-process AI worker's
state in /status, and the standalone Flask app (jobscout_server.py)."""
import pytest
import requests
from flask import Flask

from jobscout import auth, config, worker
from jobscout.api import jobscout_bp


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def remote_auth(data_dir, monkeypatch):
    """No .secret_key or users table here — like wegter-pc. Tokens are checked against a fake Surface."""
    monkeypatch.setenv("JOBS_AUTH_URL", "https://api.example.test")
    monkeypatch.setenv("JOBS_ALLOWED_EMAILS", "zweetztuph@gmail.com")
    auth._remote_cache.clear()
    calls = []
    users = {"good": {"id": 1, "email": "Zweetztuph@gmail.com", "display_name": "Michael"},
             "stranger": {"id": 2, "email": "stranger@example.com", "display_name": "S"}}

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        tok = headers["Authorization"].split(" ", 1)[1]
        if tok == "down":
            raise requests.ConnectionError("surface offline")
        if tok == "oops":
            return FakeResponse(502, None)
        return FakeResponse(200, {"user": users[tok]}) if tok in users else FakeResponse(401, {"error": "nope"})

    monkeypatch.setattr(auth.requests, "get", fake_get)
    app = Flask(__name__)
    app.register_blueprint(jobscout_bp)
    yield app.test_client(), calls
    auth._remote_cache.clear()


def _me(client, tok):
    resp = client.get("/jobs/api/me", headers={"Authorization": f"Bearer {tok}"})
    return resp.status_code, resp.get_json()


def test_remote_auth_accepts_allowlisted_user_and_caches(remote_auth):
    client, calls = remote_auth
    code, body = _me(client, "good")
    assert code == 200 and body["user"]["id"] == 1
    assert calls == ["https://api.example.test/auth/me"]
    assert _me(client, "good")[0] == 200 and len(calls) == 1  # cached: the Surface isn't asked again


def test_remote_auth_rejections(remote_auth):
    client, calls = remote_auth
    assert _me(client, "stranger") == (403, {"error": "not_allowed"})
    assert _me(client, "forged") == (401, {"error": "invalid_token"})
    _me(client, "forged")
    assert len(calls) == 2  # stranger + forged once each; the repeated forged token hit the cache
    assert _me(client, "down") == (503, {"error": "auth_unavailable"})
    assert _me(client, "oops") == (503, {"error": "auth_unavailable"})
    assert client.get("/jobs/api/me").status_code == 401  # no token at all


def test_status_folds_the_in_process_ai_worker_into_the_server(remote_auth, conn):
    client, _ = remote_auth
    worker.record_heartbeat(conn, {"worker_id": f"{config.instance()}-local", "instance": config.instance(),
                                   "role": "local-ai", "model": "google/gemma-4-12b-qat", "lmstudio_ok": True,
                                   "commit": "abc", "protocol": 1})
    conn.commit()
    status = client.get("/jobs/api/status", headers={"Authorization": "Bearer good"}).get_json()
    assert len(status["instances"]) == 1  # one machine does everything
    server = status["instances"][0]
    assert server["role"] == "jobscout" and server["online"] and server["lmstudio_ok"] is True
    assert server["model"] == "google/gemma-4-12b-qat"
    conn.execute("UPDATE workers SET last_heartbeat_at='2020-01-01T00:00:00Z'")
    conn.commit()
    stale = client.get("/jobs/api/status", headers={"Authorization": "Bearer good"}).get_json()["instances"][0]
    assert stale["lmstudio_ok"] is False  # no recent heartbeat: the AI side isn't running


def test_standalone_server_app(data_dir):
    import jobscout_server
    client = jobscout_server.app.test_client()
    assert client.get("/health").get_json() == {"status": "ok", "service": "jobscout"}
    resp = client.get("/jobs/health", headers={"Origin": "https://mwegter95.github.io"})
    assert resp.get_json()["role"] == "jobscout"
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://mwegter95.github.io"
    other = client.get("/jobs/health", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in other.headers
    assert client.get("/auth/login").status_code == 404  # nothing but Job Scout is served here


def test_start_jobscout_runs_everything_in_process(data_dir, monkeypatch):
    import jobscout_blueprint
    started = []
    monkeypatch.setattr(jobscout_blueprint.scheduler, "start_scheduler", lambda: started.append("scheduler"))
    monkeypatch.setattr(jobscout_blueprint.worker, "start_worker_thread", lambda local=False: started.append(("ai", local)))
    jobscout_blueprint.start_jobscout()
    assert started == ["scheduler", ("ai", True)]
    started.clear()
    monkeypatch.setenv("JOBS_AI_LOCAL", "0")
    jobscout_blueprint.start_jobscout()
    assert started == ["scheduler"]  # housekeeping (lease reaper) always runs
    assert config.db_path().exists()
