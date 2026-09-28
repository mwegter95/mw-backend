"""Command line: `python -m jobscout.cli <command>` (run from the mw-backend directory).

  init                                   create/migrate data/jobscout.db
  import-seeds FILE...                   bulk-import companies from seed JSON (utility; nothing ships seeded)
  plan [--industries a,b] [--keywords x,y] [--sources maps,osm] [--max-queries N]   preview a discovery plan
  discover [same options] [--cities "Oakdale, MN;Hudson, WI"]   discovery run, then pipeline for new companies
  osm                                    discovery with OpenStreetMap only
  pipeline [--company ID] [--limit N]    facts → categorize → AI task → careers/ATS → sweep for pending companies
  enrich [--company ID] [--limit N] [--all]
  detect [--company ID] [--limit N]
  sweep [--company ID] [--limit N]
  worker-once                            one AI-worker pass against MW_PRIMARY_URL
  score-local [--limit N]                run queued AI tasks in-process against JOBS_AI_API_BASE
  export-snapshot OUT.json [--user-email E]   JobDetail/CompanySummary/status/profile exactly as the API returns
  stats                                  counts
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from . import auth, config, db, discovery, pipeline, runs, sweep, tasks, views, worker


def _ids(value):
    return [int(value)] if value else None


def _csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _first_profile():
    with db.session() as conn:
        row = conn.execute("SELECT user_id FROM profiles ORDER BY id LIMIT 1").fetchone()
        return views.get_profile(conn, row["user_id"]) if row else None


def _discover_options(args):
    opts = {"industries": _csv(args.industries), "keywords": _csv(args.keywords), "sources": _csv(args.sources),
            "max_queries": args.max_queries}
    if getattr(args, "cities", None):
        opts["towns"] = [c.strip() for c in args.cities.split(";") if c.strip()]
    return opts


def cmd_plan(args):
    plan = discovery.plan_for(_first_profile(), _discover_options(args))
    print(json.dumps({"towns": plan["towns"], "total": plan["total"], "queries": plan["queries"][:20]}, indent=1))


def cmd_discover(args, sources=None):
    opts = _discover_options(args)
    if sources:
        opts["sources"] = sources
    run = runs.run_inline("discover", discovery.run_discovery, options=opts, profile=_first_profile(),
                          start_pipeline=False)
    print(json.dumps(run.stats, indent=1))
    if not args.no_pipeline:
        run = runs.run_inline("pipeline", pipeline.run_pipeline, limit=args.limit)
        print(json.dumps(run.stats, indent=1))


def cmd_score_local(args):
    w = worker.Worker(worker.LocalTransport(), worker_id="cli-local")
    done = 0
    while args.limit is None or done < args.limit:
        stats = w.run_once()
        print(stats)
        if not stats["lmstudio_ok"] or not stats["claimed"]:
            break
        done += stats["claimed"]


def cmd_export(args):
    with db.session() as conn:
        user = auth.find_user_by_email(args.user_email) if args.user_email else None
        user = user or {"id": 0, "email": args.user_email or "", "display_name": ""}
        user["id"] = int(user["id"])
        profile = views.get_profile(conn, user["id"])
        snapshot = views.export_snapshot(conn, user, profile)
    Path(args.out).write_text(json.dumps(snapshot, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}: {len(snapshot['jobs'])} jobs, {len(snapshot['companies'])} companies")


def cmd_stats(_args):
    with db.session() as conn:
        user = {"id": 0, "email": "", "display_name": ""}
        print(json.dumps(views.status(conn, user, None)["counts"], indent=1))
        print("queue", tasks.counts(conn))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(prog="python -m jobscout.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("import-seeds")
    p.add_argument("files", nargs="+")
    for name in ("plan", "discover", "osm"):
        p = sub.add_parser(name)
        p.add_argument("--industries")
        p.add_argument("--keywords")
        p.add_argument("--sources")
        p.add_argument("--max-queries", type=int)
        p.add_argument("--cities")
        p.add_argument("--limit", type=int)
        p.add_argument("--no-pipeline", action="store_true")
    for name in ("pipeline", "enrich", "detect", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--company", type=int)
        p.add_argument("--limit", type=int)
        if name == "enrich":
            p.add_argument("--all", action="store_true")
    sub.add_parser("worker-once")
    p = sub.add_parser("score-local")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("export-snapshot")
    p.add_argument("out")
    p.add_argument("--user-email")
    sub.add_parser("stats")
    args = parser.parse_args(argv)

    db.init()
    if args.cmd == "init":
        print(f"initialised {config.db_path()}")
    elif args.cmd == "import-seeds":
        for f in args.files:
            print(f, discovery.import_seeds(f))
    elif args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "discover":
        cmd_discover(args)
    elif args.cmd == "osm":
        cmd_discover(args, sources=["osm"])
    elif args.cmd == "pipeline":
        print(runs.run_inline("pipeline", pipeline.run_pipeline, company_ids=_ids(args.company), limit=args.limit).stats)
    elif args.cmd == "enrich":
        print(runs.run_inline("enrich", pipeline.run_enrich, company_ids=_ids(args.company), limit=args.limit,
                              only_pending=not args.all).stats)
    elif args.cmd == "detect":
        print(runs.run_inline("detect", pipeline.run_detect, company_ids=_ids(args.company), limit=args.limit).stats)
    elif args.cmd == "sweep":
        print(runs.run_inline("sweep", sweep.run_sweep, company_ids=_ids(args.company), limit=args.limit).stats)
    elif args.cmd == "worker-once":
        print(worker.Worker(worker.RemoteTransport()).run_once())
    elif args.cmd == "score-local":
        cmd_score_local(args)
    elif args.cmd == "export-snapshot":
        cmd_export(args)
    elif args.cmd == "stats":
        cmd_stats(args)


if __name__ == "__main__":
    main()
