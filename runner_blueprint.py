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
import logging
import datetime
import threading
import subprocess
import tempfile
from pathlib import Path

from flask import Blueprint, request, jsonify

runner_bp = Blueprint("runner", __name__, url_prefix="/run")

# Reuse the app's stdout logger so runner activity shows in the visible
# run-server.ps1 console (and any file the console is teed to).
log = logging.getLogger("mw-backend")

BASE_DIR = Path(__file__).parent
SECRET = os.environ.get("RUN_SECRET", "")
ENABLED = (os.environ.get("RUN_ENDPOINT_ENABLED", "") not in ("", "0", "false", "False")) and len(SECRET) >= 16
DEFAULT_TIMEOUT = int(os.environ.get("RUN_TIMEOUT_DEFAULT", "600"))
MAX_TIMEOUT = 1800
WORKDIR = Path(os.environ.get("RUN_WORKDIR", str(BASE_DIR / "data" / "runner-workspace")))
AUDIT_LOG = BASE_DIR / "data" / "runner-audit.log"
MAX_OUTPUT = 200_000  # bytes returned per stream
# Echo each exec's script + live output to the backend log (the visible Surface
# console). On by default so you can see what is running and its results; set
# RUN_LOG_VERBOSE=0 to silence. RUN_LOG_MAX_BYTES caps how much of a single
# stream is echoed to the log (the full output is still captured + returned).
LOG_VERBOSE = os.environ.get("RUN_LOG_VERBOSE", "1") not in ("", "0", "false", "False")
try:
    LOG_MAX_BYTES = max(0, int(os.environ.get("RUN_LOG_MAX_BYTES", "20000")))
except (TypeError, ValueError):
    LOG_MAX_BYTES = 20000
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
    label = next((ln.strip() for ln in script.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")), "")[:60]
    started = time.time()
    timed_out = False

    # Echo the script we are about to run to the backend log so it is visible in
    # the Surface console. ASCII-only markers (Windows console charmap safe).
    if LOG_VERBOSE:
        log.info("[runner %s] exec start lang=%s cwd=%s timeout=%ss cmd=%r",
                 sha, lang, cwd, timeout, label)
        log.info("[runner %s] --- script ---", sha)
        for _ln in script.splitlines():
            log.info("[runner %s] | %s", sha, _ln)
        log.info("[runner %s] --- output ---", sha)

    def _pump(stream, chunks, is_err):
        # Read the child's output line by line, append it to the capture buffer,
        # and live-echo it to the backend log (capped per stream).
        tag = "err" if is_err else "out"
        emit = log.warning if is_err else log.info
        logged = 0
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                chunks.append(line)
                if LOG_VERBOSE and logged < LOG_MAX_BYTES:
                    emit("[runner %s] %s| %s", sha, tag, line.rstrip("\n"))
                    logged += len(line)
                    if logged >= LOG_MAX_BYTES:
                        log.info("[runner %s] %s| [further %s output omitted from log; full result still returned]", sha, tag, tag)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(WORKDIR))
    out_chunks, err_chunks = [], []
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        proc = subprocess.Popen(
            [argv0, path], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
        # Threads drain both pipes concurrently (prevents the classic PIPE-full
        # deadlock) while streaming each line to the log in real time.
        t_out = threading.Thread(target=_pump, args=(proc.stdout, out_chunks, False), daemon=True)
        t_err = threading.Thread(target=_pump, args=(proc.stderr, err_chunks, True), daemon=True)
        t_out.start()
        t_err.start()
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the entire process group so spawned children (Node, postgres, etc.)
            # don't keep running and squatting ports after timeout.
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=10,
                    )
                else:
                    import signal as _sig
                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            exit_code = 124
        # Let the readers drain whatever is left before we assemble the result.
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        out = "".join(out_chunks)
        err = "".join(err_chunks)
        if timed_out:
            err = (err or "") + f"\n[timed out after {timeout}s - process tree killed]"
    except Exception as e:
        return jsonify({"ok": False, "error": f"exec failed: {e}"}), 500
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    dur = int((time.time() - started) * 1000)
    if LOG_VERBOSE:
        log.info("[runner %s] exec done exit=%s timed_out=%s dur=%sms",
                 sha, exit_code, timed_out, dur)
    _audit(f"RUN ip={_client_ip()} lang={lang} sha={sha} exit={exit_code} timeout={timed_out} dur={dur}ms cmd={label!r}")
    return jsonify({
        "ok": (exit_code == 0 and not timed_out),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": dur,
        "stdout": (out or "")[:MAX_OUTPUT],
        "stderr": (err or "")[:MAX_OUTPUT],
        "truncated": (len(out or "") > MAX_OUTPUT or len(err or "") > MAX_OUTPUT),
    })
