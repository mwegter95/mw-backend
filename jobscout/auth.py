"""Authentication for the Job Scout API.

Users sign in to michaelwegter.com (mw-backend on the Surface) and send that JWT here. Two ways to check it:
  * JOBS_AUTH_URL set (the Job Scout server on wegter-pc): ask that server's GET /auth/me, cache the
    answer for a few minutes. No signing key or accounts are copied between machines.
  * otherwise: verify locally like feelgood_blueprint.py (HS256 secret in <data>/.secret_key, users in
    <data>/mw.db) — used when Job Scout runs inside the same mw-backend, and by the tests.
Either way the email must be on the JOBS_ALLOWED_EMAILS allowlist.
Remote AI workers (optional): a shared secret in the X-Worker-Token header.
"""
import hashlib
import hmac
import sqlite3
import threading
import time
from functools import wraps

import jwt as _jwt
import requests
from flask import g, jsonify, request

from . import config

REMOTE_OK_TTL = 300    # seconds a verified token is trusted before asking again
REMOTE_BAD_TTL = 60    # seconds a rejected token stays rejected
_remote_cache = {}     # sha256(token) -> (expires_at, user | None, error code | None)
_remote_lock = threading.Lock()

_secret_cache = {}  # path -> secret text (the data dir can change in tests)


def _shared_secret():
    path = config.secret_path()
    key = str(path)
    if key not in _secret_cache:
        try:
            _secret_cache[key] = path.read_text().strip() or None
        except OSError:
            return None
    return _secret_cache[key]


def _request_token():
    """Bearer header, X-Auth-Token header, or ?_tok= (EventSource can't send headers)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Auth-Token", "") or request.args.get("_tok", "")).strip() or None


def _lookup_user(user_id):
    conn = sqlite3.connect(str(config.users_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id, email, display_name FROM users WHERE id=?", (user_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return dict(row) if row else None


def find_user_by_email(email):
    """Used by the CLI (export-snapshot --user-email)."""
    conn = sqlite3.connect(str(config.users_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id, email, display_name FROM users WHERE lower(email)=?",
                           (email.strip().lower(),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return dict(row) if row else None


def _local_user(token):
    """(user, error code, http status) using the local signing key + users table."""
    secret = _shared_secret()
    if not secret:
        return None, "auth_unavailable", 503
    try:
        payload = _jwt.decode(token, secret, algorithms=["HS256"])
    except _jwt.ExpiredSignatureError:
        return None, "token_expired", 401
    except _jwt.PyJWTError:
        return None, "invalid_token", 401
    user = _lookup_user(str(payload.get("sub") or ""))
    return (user, None, 200) if user else (None, "account_not_found", 401)


def _remote_user(token, base):
    """(user, error code, http status) by asking the issuing mw-backend's GET /auth/me (cached)."""
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    with _remote_lock:
        hit = _remote_cache.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2], (200 if hit[1] else 401)
    try:
        resp = requests.get(f"{base}/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException:
        return None, "auth_unavailable", 503  # the Surface is unreachable: don't cache
    if resp.status_code == 200:
        try:
            user = dict((resp.json() or {}).get("user") or {})
        except ValueError:
            user = {}
        entry = (now + REMOTE_OK_TTL, user, None) if user.get("id") is not None else None
    elif resp.status_code in (401, 403):
        entry = (now + REMOTE_BAD_TTL, None, "invalid_token")
    else:
        return None, "auth_unavailable", 503
    if entry is None:
        return None, "auth_unavailable", 503
    with _remote_lock:
        if len(_remote_cache) > 512:  # drop expired entries so the cache can't grow without bound
            for k in [k for k, v in _remote_cache.items() if v[0] <= now]:
                _remote_cache.pop(k, None)
        _remote_cache[key] = entry
    return entry[1], entry[2], (200 if entry[1] else 401)


def require_user(f):
    """Valid sign-in token + allowlisted email. Sets g.jobs_user = {id, email, display_name}."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = _request_token()
        if not token:
            return jsonify({"error": "auth_required"}), 401
        base = config.auth_url()
        user, error, status = _remote_user(token, base) if base else _local_user(token)
        if not user:
            return jsonify({"error": error}), status
        if (user.get("email") or "").lower() not in config.allowed_emails():
            return jsonify({"error": "not_allowed"}), 403
        user["id"] = int(user["id"])
        user["display_name"] = user.get("display_name") or ""
        g.jobs_user = user
        return f(*args, **kwargs)
    return wrapper


def require_worker(f):
    """X-Worker-Token must match JOBS_WORKER_TOKEN (constant-time); 503 when unset on the server."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        expected = config.worker_token()
        if not expected:
            return jsonify({"error": "worker_token_not_configured"}), 503
        given = request.headers.get("X-Worker-Token", "")
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return jsonify({"error": "bad_worker_token"}), 401
        return f(*args, **kwargs)
    return wrapper
