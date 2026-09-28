"""Job Scout server — runs only the Job Scout part of mw-backend.

This is what wegter-pc runs (MW_ROLE=jobscout in .env; run-server.ps1 starts this file instead of
server.py). One process does all of Job Scout's backend work: the /jobs API, the database, employer
discovery, careers-site sweeps, the nightly scheduler and the AI worker that calls LM Studio.
A Cloudflare tunnel of its own (not the Surface's mw-backend tunnel) publishes it at
https://jobs.michaelwegter.com. Sign-in still happens on the Surface; tokens are checked against its
/auth/me (JOBS_AUTH_URL). Setup: JOBSCOUT_SETUP.md.

    python jobscout_server.py            listens on 127.0.0.1:$PORT (default 5057)
"""
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from flask import Flask, jsonify  # noqa: E402  (after load_dotenv so every getenv sees .env)
from flask_cors import CORS  # noqa: E402

from jobscout_blueprint import jobscout_bp, start_jobscout  # noqa: E402

PORT = int(os.environ.get("PORT", "5057"))
# Only this machine's cloudflared (and a local browser) needs to reach the server.
HOST = os.environ.get("JOBS_BIND", "127.0.0.1")

CORS_ORIGINS = [o for o in {
    "https://mwegter95.github.io",
    "https://michaelwegter.com",
    "https://www.michaelwegter.com",
    "http://localhost:5182", "http://localhost:4180",   # Job Scout dev server / preview
    "http://localhost:5173",                            # michaelwegter.com dev server
    *[x.strip() for x in os.environ.get("JOBS_CORS_ORIGINS", "").split(",")],
} if o]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("jobscout-server")

app = Flask(__name__)
CORS(app, origins=CORS_ORIGINS)
app.register_blueprint(jobscout_bp)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "jobscout"})


if __name__ == "__main__":
    start_jobscout()
    from waitress import serve
    log.info("Job Scout server on http://%s:%s (MW_ROLE=%s)", HOST, PORT, os.environ.get("MW_ROLE", "jobscout"))
    serve(app, host=HOST, port=PORT, threads=8)
