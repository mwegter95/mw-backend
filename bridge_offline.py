"""
Shared "this demo is offline" response for the bridge blueprints.

Every bridge reverse-proxies a service running on the Surface's loopback. When
that service isn't up — Docker removed, a container stopped, a Node build
missing — urllib raises and the bridge used to answer with
`f"bridge upstream error: {e}"` as text/plain and a 502.

These URLs are linked from the work-samples page as live demos, so that string
is what a prospective client sees when they click through. A styled page that
says "temporarily offline" and points back to the portfolio costs nothing and
is a great deal less alarming than a raw ConnectionRefusedError.

503 rather than 502: the origin isn't misbehaving, it just isn't running, and
503 is the status that means "come back later" to crawlers.
"""
from flask import Response

PORTFOLIO_URL = "https://michaelwegter.com/work-samples"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{name} — demo offline</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    padding: 32px 20px;
    background: #121118; color: #f2ede4;
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    line-height: 1.6; text-align: center;
  }}
  .card {{
    max-width: 520px;
    background: #1e1c26; border: 1px solid #2c2a38; border-radius: 14px;
    padding: 40px 36px;
  }}
  .dot {{
    display: inline-block; width: 9px; height: 9px; border-radius: 50%;
    background: #e8b820; margin-right: 9px; vertical-align: middle;
  }}
  .eyebrow {{
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
    color: #e8b820; margin: 0 0 18px;
  }}
  h1 {{ margin: 0 0 12px; font-size: 24px; letter-spacing: -.02em; font-weight: 600; }}
  p {{ margin: 0 0 20px; color: #8a8898; font-size: 15px; }}
  a.btn {{
    display: inline-block; padding: 10px 20px; border-radius: 999px;
    background: #e8b820; color: #121118; font-weight: 600; font-size: 14px;
    text-decoration: none;
  }}
  a.btn:hover {{ background: #f0c73a; }}
  .muted {{
    margin: 22px 0 0; font-size: 12px; color: #4a4858;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
  }}
</style>
</head>
<body>
  <div class="card">
    <p class="eyebrow"><span class="dot"></span>Demo temporarily offline</p>
    <h1>{name}</h1>
    <p>
      This demo runs on a self-hosted machine and isn't responding right now.
      It'll be back — nothing is wrong with the project itself.
    </p>
    <a class="btn" href="{portfolio}">See the rest of the work</a>
    <p class="muted">michaelwegter.com</p>
  </div>
</body>
</html>
"""


def offline_response(demo_name, retry_after=600):
    """A presentable 503 for a demo whose upstream service isn't running."""
    html = _PAGE.format(name=demo_name, portfolio=PORTFOLIO_URL)
    return Response(
        html,
        status=503,
        mimetype="text/html",
        headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
    )
