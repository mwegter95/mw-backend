"""Job Scout — the blueprint plus its background work, for whichever Flask app hosts it.

jobscout_server.py (the Job Scout server on wegter-pc) is the host:

    from jobscout_blueprint import jobscout_bp, start_jobscout
    app.register_blueprint(jobscout_bp)          # mounted at /jobs
    start_jobscout()                             # in __main__, before serving

Importing this module starts no threads and touches no network.
"""
import logging

from jobscout import config, db, enrich, runs, scheduler, worker
from jobscout.api import jobscout_bp

__all__ = ["jobscout_bp", "start_jobscout"]

log = logging.getLogger("jobscout")


def start_jobscout():
    """Init the database, then start the scheduler (housekeeping always; nightly/monthly runs only with
    JOBS_AUTO_RUNS=1) and the in-process AI worker that talks to LM Studio (unless JOBS_AI_LOCAL=0, i.e. a
    remote worker pulls the AI tasks instead)."""
    db.init()
    with db.session() as conn:
        runs.mark_interrupted(conn)
        changed = enrich.recompute_gems(conn)  # gem rules may have changed since the last start
        if changed:
            log.info("[jobs] recomputed gem scores for %d companies", changed)
    scheduler.start_scheduler()
    if config.ai_local():
        worker.start_worker_thread(local=True)
        log.info("[jobs] AI worker started → %s (%s)", config.ai_api_base(), config.ai_model())
    else:
        log.info("[jobs] no in-process AI worker (JOBS_AI_LOCAL=0): AI tasks wait for a remote worker")
