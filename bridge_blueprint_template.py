"""
Bridge blueprint TEMPLATE — expose a local service through Flask so it is
reachable at https://api.michaelwegter.com/<PREFIX>/...

Use this when a demo runs a real service that is NOT a Flask blueprint — e.g. a
Node/Express app the Surface runner built and started on localhost. This bridge
reverse-proxies every request under /<PREFIX>/ to that local service, so the
public tunnel + CORS that already wrap Flask cover it for free.

HOW TO USE
  1. Copy this file to  <feature>_blueprint.py  (e.g. chat_bridge_blueprint.py).
  2. Set PREFIX and UPSTREAM below. PREFIX is the public path; UPSTREAM is the
     loopback address the local service listens on (started via the runner).
  3. Register it in server.py, next to the other blueprints:
         from <feature>_blueprint import bridge_bp
         app.register_blueprint(bridge_bp)
     (Rename `bridge_bp` per feature if you register more than one.)
  4. Make sure the local service is actually running on UPSTREAM. Start/verify it
     with scripts/surface_run.py from the workflow, then push this file; the
     Surface auto-deploy restarts Flask and the bridge goes live.

NOTES
  - UPSTREAM must stay on loopback (127.0.0.1). Never point this at an arbitrary
    host — it would turn the API into an open proxy.
  - CORS is applied at the app level in server.py, so it covers these routes.
  - Stdlib only (urllib); no new dependencies.
"""
import urllib.request
import urllib.error
from flask import Blueprint, request, Response

# ---- configure these two -------------------------------------------------
PREFIX = "myfeature"                 # public path  -> /myfeature/...
UPSTREAM = "http://127.0.0.1:8787"   # the local service (loopback only)
# --------------------------------------------------------------------------

bridge_bp = Blueprint(f"{PREFIX}_bridge", __name__, url_prefix=f"/{PREFIX}")

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1) + a few we recompute.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
}
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@bridge_bp.route("/", defaults={"path": ""}, methods=_METHODS)
@bridge_bp.route("/<path:path>", methods=_METHODS)
def _proxy(path):
    url = f"{UPSTREAM}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode()

    data = request.get_data() or None
    upstream_req = urllib.request.Request(url, data=data, method=request.method)
    for key, value in request.headers:
        if key.lower() not in _HOP:
            upstream_req.add_header(key, value)

    try:
        with urllib.request.urlopen(upstream_req, timeout=60) as r:
            body = r.read()
            status = r.status
            headers = [(k, v) for k, v in r.getheaders() if k.lower() not in _HOP]
    except urllib.error.HTTPError as e:
        body = e.read()
        status = e.code
        raw = e.headers.items() if e.headers else []
        headers = [(k, v) for k, v in raw if k.lower() not in _HOP]
    except Exception as e:
        return Response(f"bridge upstream error: {e}", status=502, mimetype="text/plain")

    return Response(body, status=status, headers=headers)
