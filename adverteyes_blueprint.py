"""
AdvertEyes OOH Ops Platform — bridge blueprint.
Reverse-proxies /adverteyes/... to the Node/TS service on port 3741
(started via surface_register.py, managed by run-server.ps1).
"""
import urllib.request
import urllib.error
from flask import Blueprint, request, Response

PREFIX   = "adverteyes"
UPSTREAM = "http://127.0.0.1:3741"

bridge_bp = Blueprint(f"{PREFIX}_bridge", __name__, url_prefix=f"/{PREFIX}")

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
