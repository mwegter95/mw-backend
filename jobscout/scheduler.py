"""Background scheduler for the Job Scout server (one daemon thread, 60 s tick).

Always:
* every minute: return expired AI-task leases to the queue.
Only with JOBS_AUTO_RUNS=1 (off by default — Job Scout runs on demand):
* every 30 min: categorise companies whose enrich_status is pending (facts + heuristics + AI task);
* nightly at JOBS_SWEEP_HOUR (local time; runs any time in the following 4 hours if the machine was
  busy/asleep, once per day — last date kept in meta): pipeline for pending companies, then the sweep;
* monthly on the 1st (after the sweep window opens): a discovery run from the first profile's settings.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, db, discovery, pipeline, runs, scoring, sweep, tasks

log = logging.getLogger("jobscout")

TICK_SECONDS = 60
ENRICH_EVERY = timedelta(minutes=30)
SWEEP_WINDOW_HOURS = 4

_thread = None
_last_enrich = None


def next_sweep_at(now=None) -> str:
    """ISO UTC of the next local JOBS_SWEEP_HOUR:00."""
    now = (now or datetime.now()).astimezone()
    nxt = now.replace(hour=config.sweep_hour(), minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return db.now_iso(nxt.astimezone(timezone.utc))


def _first_profile(conn):
    return scoring.profile_dict(conn.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone())


def nightly(run):
    """Pipeline for pending companies, then sweep everything not already swept by it."""
    started = db.now_iso()
    stats = pipeline.run_pipeline(run) or {}
    sweep_stats = sweep.run_sweep(run, skip_swept_since=started)
    for key, value in sweep_stats.items():
        if isinstance(value, (int, float)):
            stats[key] = stats.get(key, 0) + value
    return stats


def _start(kind, target, **kwargs):
    try:
        run = runs.start(kind, target, **kwargs)
        log.info("jobs scheduler: started %s run %s", kind, run.id)
    except runs.AlreadyRunning:
        pass


def tick(now=None):
    global _last_enrich
    now = (now or datetime.now()).astimezone()
    with db.session() as conn:
        tasks.reap(conn)
    if not config.auto_runs_enabled():
        return
    with db.session() as conn:
        today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        in_window = config.sweep_hour() <= now.hour < config.sweep_hour() + SWEEP_WINDOW_HOURS
        due_sweep = in_window and db.get_meta(conn, "last_sweep_date") != today
        # Discovery waits for a tick where no sweep starts, so both never begin at once.
        due_discover = not due_sweep and now.day == 1 and now.hour >= config.sweep_hour() and \
            db.get_meta(conn, "last_discover_month") != month
        if due_sweep:
            db.set_meta(conn, "last_sweep_date", today)
        if due_discover:
            db.set_meta(conn, "last_discover_month", month)
        profile = _first_profile(conn) if due_discover else None
    if due_sweep:
        _start("sweep", nightly)
    elif due_discover:
        _start("discover", discovery.run_discovery, profile=profile)
    if _last_enrich is None or now - _last_enrich >= ENRICH_EVERY:
        _last_enrich = now
        with db.session() as conn:
            pending = conn.execute("SELECT COUNT(*) n FROM companies WHERE enrich_status='pending' "
                                   "AND status != 'ignored'").fetchone()["n"]
        if pending:
            _start("enrich", pipeline.run_enrich, limit=25)


def _loop():
    if config.auto_runs_enabled():
        log.info("jobs scheduler started: automatic runs on (sweep hour %s local)", config.sweep_hour())
    else:
        log.info("jobs scheduler started: on demand only (JOBS_AUTO_RUNS=1 enables nightly/monthly runs)")
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — keep the scheduler alive
            log.warning("jobs scheduler tick failed: %s", exc)
        time.sleep(TICK_SECONDS)


def start_scheduler():
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_loop, name="jobs-scheduler", daemon=True)
    _thread.start()
    return _thread
