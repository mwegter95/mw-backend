"""Job Scout — thin shim so server.py can register the blueprint and start background work.

    from jobscout_blueprint import jobscout_bp, start_jobscout
    app.register_blueprint(jobscout_bp)          # mounted at /jobs
    start_jobscout(os.environ.get("MW_ROLE", "primary"))

Importing this module starts no threads and touches no network.
"""
import logging

from jobscout import config, db, enrich, runs, scheduler, worker
from jobscout.api import jobscout_bp

__all__ = ["jobscout_bp", "start_jobscout"]

log = logging.getLogger("jobscout")


def start_jobscout(role=None):
    """primary: init DB, start the scheduler (unless JOBS_SCHEDULER=0) and, with JOBS_AI_LOCAL=1, an
    in-process AI worker. ai-worker: start the worker loop that pulls tasks from MW_PRIMARY_URL."""
    role = (role or config.role()).lower()
    if role == "ai-worker":
        worker.start_worker_thread()
        log.info("[jobs] ai-worker started → %s", config.primary_url())
        return
    db.init()
    with db.session() as conn:
        runs.mark_interrupted(conn)
        changed = enrich.recompute_gems(conn)  # gem rules may have changed since the last start
        if changed:
            log.info("[jobs] recomputed gem scores for %d companies", changed)
    if config.scheduler_enabled():
        scheduler.start_scheduler()
    else:
        log.info("[jobs] scheduler disabled (JOBS_SCHEDULER=0)")
    if config.ai_local():
        worker.start_worker_thread(local=True)
        log.info("[jobs] in-process AI worker started (JOBS_AI_LOCAL=1)")
