""""Find matches": the one on-demand run that does everything, in order.

1. discover — search Google Maps / the web / OpenStreetMap for employers not seen before
   (the person's "what to hunt for" settings; skipped with options.skip_discovery),
   then categorize each new one from its own website          (progress phases "discover", "categorize")
2. pipeline — for every company not processed yet: careers page, careers system, first job read
                                                               (phase "pipeline")
3. sweep — read open jobs at every other company with a careers system          (phase "sweep")
4. queue AI scoring for jobs in each person's field; the AI works through it in the background and
   GET /jobs/api/status → activity.ai reports how far it has got.

Each step logs a "step n of 3" line and reports progress, which the app's activity panel shows.
"""
from . import db, discovery, interests, pipeline, sweep

STAT_KEYS = ("queries", "candidates", "new_companies", "companies_checked", "companies_read", "jobs_seen",
             "jobs_new", "matching_new", "score_tasks", "errors")


def run_find(run, options=None, profile=None, fetcher=None):
    opts = dict(options or {})
    started = db.now_iso()
    with db.session() as conn:
        wanted = interests.combined(conn)
    if wanted.empty:
        run.log("No job categories or target titles in any profile yet: companies will be found and categorized, "
                "but no jobs can be matched. Add them in Settings → Profile.")
    stats = {k: 0 for k in STAT_KEYS}

    steps = 2 if opts.get("skip_discovery") else 3
    n = 0
    if not opts.get("skip_discovery"):
        n += 1
        run.log(f"step {n} of {steps}: looking for employers you haven't seen yet")
        found = discovery.run_discovery(run, options=opts, profile=profile, fetcher=fetcher, start_pipeline=False)
        for key in ("queries", "candidates", "new_companies"):
            stats[key] += found.get(key, 0) or 0

    n += 1
    run.log(f"step {n} of {steps}: checking careers pages of new companies")
    checked = pipeline.run_pipeline(run, fetcher=fetcher) or {}
    stats["companies_checked"] += checked.get("companies", 0) or 0

    n += 1
    run.log(f"step {n} of {steps}: reading open jobs at every company with a careers page")
    swept = sweep.run_sweep(run, fetcher=fetcher, skip_swept_since=started) or {}
    stats["companies_read"] += swept.get("companies", 0) or 0

    for part in (checked, swept):
        for key in ("jobs_seen", "jobs_new", "matching_new", "score_tasks", "errors"):
            stats[key] += part.get(key, 0) or 0
    run.log(f"done: {stats['new_companies']} new companies, {stats['matching_new']} new jobs in your field; "
            f"the AI is scoring {stats['score_tasks']} of them now")
    return stats
