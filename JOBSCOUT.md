# Job Scout backend — developer notes

Job Scout's backend, which runs by itself on wegter-pc (`jobscout_server.py`). The contract (API shapes, schema, AI schemas, taxonomies) lives in the
ultimate-job-scraper repo's `docs/CONTRACT.md`; running the two instances is in `JOBSCOUT_SETUP.md`; this file explains how the code is organised.

## Wiring

Job Scout runs on its own: `jobscout_server.py` is a small Flask app (CORS for the front end, `/health`, the
`/jobs` blueprint) served by waitress on 127.0.0.1:$PORT (5057), published through wegter-pc's own Cloudflare
tunnel as jobs.michaelwegter.com. `server.py` (the Surface) does not load Job Scout. `run-server.ps1` with
`MW_ROLE=jobscout` starts `jobscout_server.py`, skips the mw-backend tunnel and managed services, and installs
`requirements.jobscout.txt` on auto-deploy. Setup: `JOBSCOUT_SETUP.md`.

```python
from jobscout_blueprint import jobscout_bp, start_jobscout
app.register_blueprint(jobscout_bp)
start_jobscout()      # db.init, scheduler (housekeeping; automatic runs only with JOBS_AUTO_RUNS=1),
                      # in-process AI worker (unless JOBS_AI_LOCAL=0)
```
Importing the shim starts nothing. Data lives in `JOBSCOUT_DATA_DIR` (default `mw-backend/data`): `jobscout.db`.

**Sign-in**: the front end logs in on the Surface (api.michaelwegter.com/auth/login) and sends that token here.
With `JOBS_AUTH_URL` set, `auth.require_user` verifies it by calling `{JOBS_AUTH_URL}/auth/me` (answers cached
5 minutes, rejections 1 minute, never cached when the Surface is unreachable → 503 `auth_unavailable`). Without
it, it verifies locally with `<data>/.secret_key` and `<data>/mw.db` (the tests do this).

**AI**: the worker thread inside the server claims tasks from the local queue and calls LM Studio. Optionally
the AI can run on another machine instead: `JOBS_AI_LOCAL=0` + `JOBS_WORKER_TOKEN` on the server, and
`python -m jobscout.worker` there with `MW_PRIMARY_URL=https://jobs.michaelwegter.com` and the same token
(it pulls tasks over `/jobs/worker/*`). Not used in the current setup.

## Module map (`jobscout/`)

| Module | Purpose |
|---|---|
| `config.py` | env vars (read at call time), `PROTOCOL`, home coordinates, commit SHA |
| `db.py` | SQLite connect (WAL, FKs), idempotent schema + additive `ALTER TABLE` migrations, helpers |
| `taxonomy.py` | industries and every enum shared with the frontend; `/meta` payload |
| `auth.py` | `require_user` (JWT + allowlist) and `require_worker` (X-Worker-Token) |
| `http.py` | `PoliteFetcher`: Chrome headers, per-host spacing (3 s pages / 1 s ATS APIs) across threads, retries, robots.txt for pages, `Blocked` on 403/challenges |
| `browser.py` | optional Playwright render (`fetch_rendered`), `page_html` = static first, browser on `Blocked` |
| `interests.py` | job categories (id, label, title patterns, search terms), a person's interests (categories + target titles + levels) → title match pass/maybe/fail; `combined` = everyone's, stored as `jobs.prefilter` |
| `normalize.py` | HTML sanitise/text, title tier, rule score (contract §4), salary parser, workplace, locations, hashes |
| `geo.py` | offline gazetteer (`data/places.json`, MN/WI/IA/ND/SD), Census geocoder, haversine, ORS drive minutes |
| `ats/` | `detect_from_url/html` for every contract ats_type; adapters: workday, oracle, greenhouse, lever, ashby, smartrecruiters, bamboohr, breezy, recruitee, paylocity, workable, jsonld; any other detected system is read by the AI |
| `careers.py` | find the careers page and the ATS behind it → company status (`active`/`no_ats`/`no_careers`/`blocked`/`manual_check`) |
| `enrich.py` | site facts, heuristic categoriser (industry, entity_type, local_presence, ownership…), AI enrichment apply, gem score, auto-ignore |
| `discovery.py` | discovery plan (terms × towns within radius), sources maps/search (Playwright via clientfinder), osm (Overpass), places (Google, optional); candidate filtering; `import_seeds` utility |
| `pipeline.py` | per-company pipeline: facts → heuristics → AI task → careers/ATS → sweep; run kinds pipeline/enrich/detect/company |
| `sweep.py` | job sweep: list, local filter, detail for new/changed pass/maybe, normalise, upsert, close after 2 misses; `html` careers pages → `parse_page` |
| `scoring.py` | profile/job hashes, rule vs AI fit resolution, score payloads/results, `enqueue_scores` |
| `ai_schemas.py` / `ai.py` | strict JSON schemas + validation/clamping; LM Studio client and prompts |
| `tasks.py` | AI queue: enqueue (deduped), atomic claim with 10-min lease, complete/fail, lease reaper |
| `worker.py` | AI worker loop (remote or local transport), heartbeat, protocol-mismatch handling |
| `runs.py` | background runs with live event history for SSE (one running run per kind) |
| `find.py` | the on-demand "Find matches" run: discover → categorize → careers pages → read jobs → queue AI scoring |
| `scheduler.py` | lease reaper always; with `JOBS_AUTO_RUNS=1` also 30-min categorisation, nightly pipeline+sweep, monthly discovery |
| `views.py` / `api.py` | response builders (shared with `export-snapshot`) and the Flask blueprint |
| `cli.py` | command line |

## CLI (run from the mw-backend directory)

```
python -m jobscout.cli init
python -m jobscout.cli plan --keywords "commercial printing" --max-queries 20
python -m jobscout.cli discover [--industries a,b] [--keywords x,y] [--sources maps,search,osm,places] [--max-queries 60] [--cities "Oakdale, MN;Hudson, WI"] [--no-pipeline]
python -m jobscout.cli osm
python -m jobscout.cli pipeline [--company ID] [--limit N]
python -m jobscout.cli enrich [--company ID] [--limit N] [--all]
python -m jobscout.cli detect [--company ID] [--limit N]
python -m jobscout.cli sweep [--company ID] [--limit N]
python -m jobscout.cli worker-once                 # one worker pass against MW_PRIMARY_URL
python -m jobscout.cli score-local [--limit N]     # run queued AI tasks here against JOBS_AI_API_BASE
python -m jobscout.cli import-seeds FILE...        # bulk import (utility; nothing ships seeded)
python -m jobscout.cli export-snapshot OUT.json [--user-email E]
python -m jobscout.cli stats
python -m jobscout.worker --once [--local]
python -m jobscout.geo build 2024_Gaz_place_national.txt 2024_Gaz_cousubs_national.txt   # rebuild the gazetteer
```

## How companies and jobs flow

1. **Discovery** (`discover` run, monthly or from the UI): the plan crosses the profile's discover industries' query
   phrases and free-text keywords with towns inside the radius. Each source returns candidates; domains are normalised,
   filtered (aggregators, national brands, junk, `.gov`/`.xx.us`) and deduped. New ones become `source='discovery'`,
   `status='pending'`, then immediately get pipeline steps 1–2, and a `pipeline` run is started for steps 3–4.
   * **maps** — `discovery._maps_search` reads Google Maps' results feed directly: each card carries the place link
     (name + `!3d…!4d…` coordinates), a "Website" button and a "category · street · phone" line, so nothing is clicked.
     (clientfinder's Maps helper clicks six cards and its selectors went stale; Job Scout no longer uses it.)
   * **search** — clientfinder's DuckDuckGo/Bing/Yellow Pages helpers. Bare keywords search as businesses
     (`business_phrase`: "commercial printing" → "commercial printing company"; a bare product word tends to return
     articles about it). With no industries or keywords, the plan uses one phrase for every industry (`broad_terms`).
   * **osm** — one Overpass bounding-box query with a single key-regex per website tag, trimmed to the radius in
     Python (a large `around:` union of many selectors 504s on the public servers), sent with the descriptive
     `http.APP_UA` (overpass-api.de answers 406 to generic and browser user agents). Storefront/solo office kinds
     (`_OSM_SKIP_CATEGORIES`: realtors, insurance agents, locksmiths…) are dropped before categorizing.
2. **Pipeline** per company: homepage (+ about page) facts → HQ geocode → heuristic categorisation (so the UI is useful
   while wegter-pc is off) → `enrich_company` AI task → careers page + ATS detection → sweep. AI results overwrite the
   heuristics (never seed values), recompute the gem score and auto-ignore directories/news, article pages, chain
   branches and sites with no sign of a local presence (unless a user restored the company).
   * The heuristic only picks a manufacturing industry with manufacturing evidence (`enrich.makes_things`), so a
     roofing installer is construction, not building materials.
   * **Hidden gem** = an established local employer most people haven't heard of, in **any industry**:
     gem_score ≥ 70, local HQ/major office, not well known, 50–4,999 people, **and categorized by the AI** —
     keywords alone can't judge size or fame. Gem fields are recomputed for every company at server startup
     (`enrich.recompute_gems`).
3. **Sweep** (on demand — part of "Find matches"; nightly only with `JOBS_AUTO_RUNS=1`): each active company's adapter
   lists jobs, searching big boards with the words from everyone's job categories and target titles
   (`interests.combined(conn).search_terms()`). Jobs outside MN/WI (and not US-wide remote) are dropped before any
   detail fetch. Titles are rated pass/maybe/fail against everyone's interests (stored `jobs.prefilter`); each
   person's list and AI scoring then use their own (`interests.for_profile`). Details are fetched only for pass/maybe titles that are new,
   retitled, or older than 14 days. A careers system without an adapter (Paycom, iCIMS…) is swept like a plain
   careers page: the rendered board text goes to a `parse_page` AI task. Salary comes from structured ATS data, else the salary text, else the description.
   Jobs missing from two consecutive sweeps get `closed_at`. Finally `enqueue_scores` queues `score_job` for every
   (open pass/maybe job × profile) whose AI score hash no longer matches `hash(profile.input_hash, job.content_hash)`.
4. **AI worker** (a thread in the same server) claims tasks (score_job > enrich_company > parse_page), calls LM Studio
   with a strict json_schema, and the results are validated/clamped and applied. Until then the API shows rule
   scores and keyword categories. `ai.parse_json_content` strips a reasoning block if the model's thinking is on.

## Adding an ATS adapter

1. Add a URL pattern to `_MATCHERS` in `ats/__init__.py` (if the type isn't already detected).
2. Create `ats/<type>.py` with a subclass of `ats.base.Adapter`:
   * `list_jobs(company, keywords) -> [RawJob]` — cheap listing; set `raw.detailed=True` when descriptions are included;
   * `get_detail(company, raw) -> RawJob` and `has_detail = True` when the listing lacks descriptions/pay.
   Put raw ATS values in `RawJob` (location text, workplace value, salary min/max/period or text); normalisation is shared.
3. Register it in `_registry()` in `ats/__init__.py`. Until then those boards are read by the AI (status_reason
   "<type> board read by AI (no adapter yet)"); once registered, the next sweep uses the adapter automatically.
4. Record a trimmed live response under `tests/fixtures/` and add a test in `tests/test_jobscout_ats.py` using `FakeFetcher`.

## Tests

`python -m pytest -q tests/test_jobscout_*.py` — offline (fixtures recorded from live APIs on 2026-09-27; Overpass/Places
and HTML samples are synthetic).
