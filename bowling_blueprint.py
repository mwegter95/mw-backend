"""
Bowling Shirt Designer -- Flask Blueprint
Mounts under /bowling on mw-backend.

If a PHP microservice (php -S 127.0.0.1:5060) is running, all requests are
proxied to it (BOWLING_PHP_AVAILABLE=1). Otherwise the blueprint serves the
same endpoints natively so the demo works regardless.

Endpoints:
  GET  /bowling/health           liveness + mode
  GET  /bowling/patterns         list of pattern names
  POST /bowling/session          save customization JSON -> {id}
  GET  /bowling/session/<id>     reload saved customization
"""

import os
import json
import time
import secrets
import pathlib

from flask import Blueprint, request, jsonify, Response
import urllib.request
import urllib.error

bowling_bp = Blueprint("bowling", __name__, url_prefix="/bowling")

_PHP_UPSTREAM = "http://127.0.0.1:5060"
_SESSIONS_DIR = pathlib.Path(__file__).parent / "data" / "bowling_sessions"
_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

_PATTERNS = ["none", "starburst", "pins", "argyle", "polka", "chevrons", "bowtie"]

_HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding",
        "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
        "host", "content-length"}


def _php_available() -> bool:
    return os.environ.get("BOWLING_PHP_AVAILABLE") == "1"


def _proxy(path: str):
    url = f"{_PHP_UPSTREAM}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode()
    data = request.get_data() or None
    req = urllib.request.Request(url, data=data, method=request.method)
    for k, v in request.headers:
        if k.lower() not in _HOP:
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(); status = r.status
            hdrs = [(k, v) for k, v in r.getheaders() if k.lower() not in _HOP]
    except urllib.error.HTTPError as e:
        body = e.read(); status = e.code
        hdrs = [(k, v) for k, v in (e.headers.items() if e.headers else []) if k.lower() not in _HOP]
    except Exception as e:
        return Response(f"PHP upstream error: {e}", status=502, mimetype="text/plain")
    return Response(body, status=status, headers=hdrs)


@bowling_bp.route("/health")
def health():
    mode = "php-proxied" if _php_available() else "native-flask"
    return jsonify({"status": "ok", "mode": mode})


@bowling_bp.route("/patterns")
def patterns():
    if _php_available():
        return _proxy("patterns")
    return jsonify({"patterns": _PATTERNS})


@bowling_bp.route("/session", methods=["POST"])
def save_session():
    if _php_available():
        return _proxy("session")
    data = request.get_json(force=True, silent=True) or {}
    sid = secrets.token_urlsafe(8)
    (_SESSIONS_DIR / f"{sid}.json").write_text(
        json.dumps({"id": sid, "ts": int(time.time()), "data": data})
    )
    return jsonify({"id": sid})


@bowling_bp.route("/session/<sid>")
def load_session(sid: str):
    if _php_available():
        return _proxy(f"session/{sid}")
    p = _SESSIONS_DIR / f"{sid}.json"
    if not p.exists():
        return jsonify({"error": "session not found"}), 404
    return jsonify(json.loads(p.read_text()))
