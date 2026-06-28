"""
Bridge blueprint — Maryland Driveway Restore WordPress demo
Reverse-proxies /demos/maryland-driveway-restore/* to the local WP Docker container
running at 127.0.0.1:8081.

Registered in server.py as mdr_wp_bridge_bp.
"""
import urllib.request
import urllib.error
from flask import Blueprint, request, Response

PREFIX   = "demos/maryland-driveway-restore"
UPSTREAM = "http://127.0.0.1:8081"

bridge_bp = Blueprint("mdr_wp_bridge", __name__, url_prefix=f"/{PREFIX}")

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1)
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding", "accept-encoding",
}
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects — pass them straight through to the client."""
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


@bridge_bp.route("/", defaults={"path": ""}, methods=_METHODS)
@bridge_bp.route("/<path:path>", methods=_METHODS)
def _proxy(path):
    url = f"{UPSTREAM}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8", errors="replace")

    data = request.get_data() or None
    upstream_req = urllib.request.Request(url, data=data, method=request.method)
    for key, value in request.headers:
        if key.lower() not in _HOP:
            upstream_req.add_header(key, value)
    # Inject canonical Host so WP uses the right base URL (not 127.0.0.1:8081)
    upstream_req.add_unredirected_header("Host", "api.michaelwegter.com")
    upstream_req.add_unredirected_header("X-Forwarded-Proto", "https")

    try:
        with _opener.open(upstream_req, timeout=30) as r:
            body = r.read()
            status = r.status
            headers = [(k, v) for k, v in r.getheaders() if k.lower() not in _HOP]
    except urllib.error.HTTPError as e:
        body = e.read()
        status = e.code
        raw = e.headers.items() if e.headers else []
        headers = [(k, v) for k, v in raw if k.lower() not in _HOP]
    except Exception as e:
        return Response(
            f"MDR bridge upstream error: {e}",
            status=502,
            mimetype="text/plain"
        )

    return Response(body, status=status, headers=headers)
