# Job Scout backend — developer notes

Blueprint `/jobs` inside mw-backend. The contract (API shapes, schema, AI schemas, taxonomies) lives in the
ultimate-job-scraper repo's `docs/CONTRACT.md`; running the two instances is in `JOBSCOUT_SETUP.md`; this file explains how the code is organised.

## Wiring (server.py)

```python
from jobscout_blueprint import jobscout_bp, start_jobscout
app.register_blueprint(jobscout_bp)
start_jobscout(os.environ.get("MW_ROLE", "primary"))   # in __main__, after init_db()
```
Importing the shim starts nothing. `start_jobscout("primary")` runs `db.init()`, marks interrupted runs failed, starts
the scheduler (unless `JOBS_SCHEDULER=0`) and, with `JOBS_AI_LOCAL=1`, an in-process AI worker.
`start_jobscout("ai-worker")` only starts the worker loop that pulls tasks from `MW_PRIMARY_URL`.
Data lives in `JOBSCOUT_DATA_DIR` (default `mw-backend/data`): `jobscout.db`, plus the shared `mw.db` and `.secret_key`.

## Module map (`jobscout/`)

| Module | Purpose |
|---|---|
| `config.py` | env vars (read at call time), `PROTOCOL`, home coordinates, function keywords, commit SHA |
| `db.py` | SQLite connect (WAL, FKs), idempotent schema + additive `ALTER TABLE` migrations, helpers |
| `taxonomy.py` | industries and every enum shared with the frontend; `/meta` payload |
| `auth.py` | `require_user` (JWT + allowlist) and `require_worker` (X-Worker-Token) |
| `http.py` | `PoliteFetcher`: Chrome headers, per-host spacing (3 s pages / 1 s ATS APIs) across threads, retries, robots.txt for pages, `Blocked` on 403/challenges |
| `browser.py` | optional Playwright render (`fetch_rendered`), `page_html` = static first, browser on `Blocked` |
| `normalize.py` | HTML sanitise/text, title tier + prefilter + rule score (contract §4), salary parser, workplace, locations, hashes |
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
| `scheduler.py` | lease reaper, 30-min categorisation, nightly pipeline+sweep, monthly discovery |
| `views.py` / `api.py` | response builders (shared with `export-snapshot`) and the Flask blueprint |
| `cli.py` | command line |

## CLI (run from the mw-backend directory)

```
python -m jobscout.cli init
python -m jobscout.cli plan --keywords "injection molding" --max-queries 20
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
     (`business_phrase`: "injection molding" → "injection molding company"; the bare phrase returns medical articles).
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
   * **Hidden gem** = gem_score ≥ 70, local HQ/major office, not well known, a maker (`taxonomy.is_maker`:
     manufacturing or distribution) **and categorized by the AI** — keywords alone can't judge size or fame. Gem
     fields are recomputed for every company at primary startup (`enrich.recompute_gems`).
3. **Sweep** (nightly): each active company's adapter lists jobs with the function keywords. Jobs outside MN/WI (and not
   US-wide remote) are dropped before any detail fetch. Details are fetched only for pass/maybe titles that are new,
   retitled, or older than 14 days. A careers system without an adapter (Paycom, iCIMS…) is swept like a plain
   careers page: the rendered board text goes to a `parse_page` AI task. Salary comes from structured ATS data, else the salary text, else the description.
   Jobs missing from two consecutive sweeps get `closed_at`. Finally `enqueue_scores` queues `score_job` for every
   (open pass/maybe job × profile) whose AI score hash no longer matches `hash(profile.input_hash, job.content_hash)`.
4. **AI worker** claims tasks (score_job > enrich_company > parse_page), calls LM Studio with a strict json_schema and
   posts results back; the primary validates/clamps and applies them. Until then the API shows rule scores.

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
