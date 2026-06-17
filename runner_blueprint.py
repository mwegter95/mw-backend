"""
Remote runner — Flask Blueprint (mounts under /run).

WHAT IT IS: lets an authenticated caller run a python or bash script ON THIS HOST
and get back stdout/stderr/exit code. It powers the Upwork workflow's ability to
build and deploy REAL backends on the Surface (install runtimes, build services,
start them) instead of mocking.

SECURITY (read this): this executes arbitrary code on the machine. It is OFF
unless BOTH of these are set in mw-backend's .env:
    RUN_ENDPOINT_ENABLED=1
    RUN_SECRET=<a long random secret, 32+ chars>
Requests must carry an HMAC-SHA256 signature over "<timestamp>.<raw-body>" using
RUN_SECRET, with a 60-second timestamp window (replay protection). The secret is
never sent over the wire. Treat RUN_SECRET like an SSH private key. For a second
layer, put Cloudflare Access in front of the /run path.

Optional .env keys:
    RUN_TIMEOUT_DEFAULT=600     default per-script timeout (seconds), hard cap 1800
    RUN_WORKDIR=<abs path>      default working dir (default: data/runner-workspace)
    RUN_PYTHON=<abs path>       python to use (default: ./venv python, else current)
"""

import os
import hmac
import time
import json
import hashlib
import datetime
import subprocess
import tempfile
from pathlib import Path

from flask import Blueprint, request, jsonify

runner_bp = Blueprint("runner", __name__, url_prefix="/run")

BASE_DIR = Path(__file__).parent
SECRET = os.environ.get("RUN_SECRET", "")
ENABLED = (os.environ.get("RUN_ENDPOINT_ENABLED", "") not in ("", "0", "false", "False")) and len(SECRET) >= 16
DEFAULT_TIMEOUT = int(os.environ.get("RUN_TIMEOUT_DEFAULT", "600"))
MAX_TIMEOUT = 1800
WORKDIR = Path(os.environ.get("RUN_WORKDIR", str(BASE_DIR / "data" / "runner-workspace")))
AUDIT_LOG = BASE_DIR / "data" / "runner-audit.log"
MAX_OUTPUT = 200_000  # bytes returned per stream
try:
    WORKDIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def _venv_python():
    import sys
    cand = BASE_DIR / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable


def _client_ip():
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()))


def _audit(line):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(f"{datetime.datetime.utcnow().isoformat()}Z {line}\n")
    except Exception:
        pass


def _authorized(raw_body):
    if not ENABLED:
        return False, "runner disabled"
    ts = request.headers.get("X-Run-Timestamp", "")
    sig = request.headers.get("X-Run-Signature", "")
    if not ts or not sig:
        return False, "missing signature headers"
    try:
        skew = abs(time.time() - int(ts))
    except ValueError:
        return False, "bad timestamp"
    if skew > 60:
        return False, "stale timestamp"
    expected = hmac.new(SECRET.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "bad signature"
    return True, ""


@runner_bp.get("/health")
def health():
    return jsonify({"ok": True, "enabled": ENABLED})


@runner_bp.post("/exec")
def execute():
    raw = request.get_data() or b""
    ok, why = _authorized(raw)
    if not ok:
        _audit(f"DENY ip={_client_ip()} reason={why}")
        return jsonify({"ok": False, "error": why}), (403 if why == "runner disabled" else 401)

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return jsonify({"ok": False, "error": "invalid JSON"}), 400

    script = body.get("script", "")
    if not isinstance(script, str) or not script.strip():
        return jsonify({"ok": False, "error": "missing script"}), 400
    lang = (body.get("language") or "python").lower()
    try:
        timeout = min(int(body.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    cwd = body.get("cwd") or str(WORKDIR)

    if lang == "python":
        suffix, argv0 = ".py", (os.environ.get("RUN_PYTHON") or _venv_python())
    elif lang in ("bash", "sh", "shell"):
        suffix, argv0 = ".sh", "bash"
    else:
        return jsonify({"ok": False, "error": f"unsupported language: {lang}"}), 400

    sha = hashlib.sha256(script.encode()).hexdigest()[:12]
    started = time.time()
    timed_out = False
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(WORKDIR))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        try:
            proc = subprocess.run(
                [argv0, path], cwd=cwd, capture_output=True, text=True,
                timeout=timeout, start_new_session=True,
            )
            exit_code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = 124
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode() if e.stdout else "")
            err = (e.stderr if isinstance(e.stderr, str) else (e.stderr.decode() if e.stderr else "")) + f"\n[timed out after {timeout}s]"
    except Exception as e:
        return jsonify({"ok": False, "error": f"exec failed: {e}"}), 500
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    dur = int((time.time() - started) * 1000)
    _audit(f"RUN ip={_client_ip()} lang={lang} sha={sha} exit={exit_code} timeout={timed_out} dur={dur}ms")
    return jsonify({
        "ok": (exit_code == 0 and not timed_out),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": dur,
        "stdout": (out or "")[:MAX_OUTPUT],
        "stderr": (err or "")[:MAX_OUTPUT],
        "truncated": (len(out or "") > MAX_OUTPUT or len(err or "") > MAX_OUTPUT),
    })
