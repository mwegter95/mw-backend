"""
Freight Factoring Gateway — Flask Blueprint
Reverse-proxies /api/factoring/* -> NestJS at http://localhost:3001/*
Uses stdlib urllib.request only (zero new deps).
"""
import urllib.request as _req
import urllib.error
from flask import Blueprint, request, Response

factoring_gw_bp = Blueprint("factoring_gw", __name__, url_prefix="/api/factoring")

NEST_BASE = "http://localhost:3001"
FORWARD_HEADERS = {"authorization", "content-type", "accept", "cookie", "x-request-id"}


def _proxy(path=""):
    url = f"{NEST_BASE}/{path}"
    qs = request.query_string.decode("utf-8")
    if qs:
        url = f"{url}?{qs}"
    headers = {k: v for k, v in request.headers if k.lower() in FORWARD_HEADERS}
    body = request.get_data() or None
    upstream = _req.Request(url, data=body, headers=headers, method=request.method)
    try:
        with _req.urlopen(upstream, timeout=30) as resp:
            ct = resp.headers.get("Content-Type", "application/json")
            return Response(resp.read(), status=resp.status, content_type=ct)
    except urllib.error.HTTPError as exc:
        ct = exc.headers.get("Content-Type", "application/json")
        return Response(exc.read(), status=exc.code, content_type=ct)
    except Exception as exc:
        return Response(f"Gateway error: {exc}", status=502, content_type="text/plain")


@factoring_gw_bp.route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@factoring_gw_bp.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy(path=""):
    if request.method == "OPTIONS":
        # Return CORS preflight directly without hitting NestJS (works even when upstream is down)
        resp = Response("", status=204)
        origin = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization,Content-Type,Accept,X-Request-Id"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp
    return _proxy(path)
