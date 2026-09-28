"""Authentication for the Job Scout API.

Users: the shared mw-backend JWT (HS256, secret in <data>/.secret_key, users in <data>/mw.db),
exactly like feelgood_blueprint.py, plus the JOBS_ALLOWED_EMAILS allowlist.
Workers: a shared secret in the X-Worker-Token header.
"""
import hmac
import sqlite3
from functools import wraps

import jwt as _jwt
from flask import g, jsonify, request

from . import config

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


def require_user(f):
    """Valid JWT + allowlisted email. Sets g.jobs_user = {id, email, display_name}."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        secret = _shared_secret()
        if not secret:
            return jsonify({"error": "auth_unavailable"}), 503
        token = _request_token()
        if not token:
            return jsonify({"error": "auth_required"}), 401
        try:
            payload = _jwt.decode(token, secret, algorithms=["HS256"])
        except _jwt.ExpiredSignatureError:
            return jsonify({"error": "token_expired"}), 401
        except _jwt.PyJWTError:
            return jsonify({"error": "invalid_token"}), 401
        user = _lookup_user(str(payload.get("sub") or ""))
        if not user:
            return jsonify({"error": "account_not_found"}), 401
        if (user["email"] or "").lower() not in config.allowed_emails():
            return jsonify({"error": "not_allowed"}), 403
        user["id"] = int(user["id"])
        user["display_name"] = user.get("display_name") or ""
        g.jobs_user = user
        return f(*args, **kwargs)
    return wrapper


def require_worker(f):
    """X-Worker-Token must match JOBS_WORKER_TOKEN (constant-time); 503 when unset on the primary."""
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
