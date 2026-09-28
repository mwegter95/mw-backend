"""Run registry: background jobs (sweep, discover, pipeline…) with live progress for SSE.

Each run is a row in `runs` plus an in-memory `Run` that keeps every event so late subscribers
replay the history. Only one run per key (normally the kind; "company:<id>" for company runs)
may be running at a time in this process.
"""
import logging
import queue
import threading
import traceback

from . import db

log = logging.getLogger("jobscout")

_lock = threading.Lock()
_active = {}      # key -> Run
_recent = {}      # id -> Run (kept for streaming after finish)
_RECENT_MAX = 30


class AlreadyRunning(Exception):
    def __init__(self, run_id):
        super().__init__(f"run {run_id} already running")
        self.run_id = run_id


class Run:
    def __init__(self, run_id, kind, key):
        self.id, self.kind, self.key = run_id, kind, key
        self.stats = {}
        self.status = "running"
        self.events = []
        self._subscribers = []
        self._lock = threading.Lock()

    # ── events ───────────────────────────────────────────────────────────────
    def _emit(self, event):
        with self._lock:
            self.events.append(event)
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)

    def log(self, line):
        line = str(line)[:500]
        log.info("[jobs run %s %s] %s", self.id, self.kind, line)
        with db.session() as conn:
            conn.execute("UPDATE runs SET log = COALESCE(log, '') || ? WHERE id=?", (line + "\n", self.id))
        self._emit({"type": "log", "line": line})

    def progress(self, done, total, phase):
        self._emit({"type": "progress", "done": done, "total": total, "phase": phase})

    def add(self, key, n=1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def merge(self, stats):
        for key, value in (stats or {}).items():
            if isinstance(value, dict):
                with self._lock:
                    bucket = self.stats.setdefault(key, {})
                    for k, v in value.items():
                        bucket[k] = bucket.get(k, 0) + v
            elif isinstance(value, (int, float)):
                self.add(key, value)

    def finish(self, status, stats=None):
        if stats:
            self.stats.update(stats)
        with db.session() as conn:
            db.update(conn, "runs", "id", self.id, {"status": status, "finished_at": db.now_iso(),
                                                    "stats": db.dumps(self.stats)})
        with _lock:
            _active.pop(self.key, None)
        done = {"type": "done", "status": status, "stats": self.stats}
        with self._lock:  # status flip + final event are atomic w.r.t. subscribe()
            self.status = status
            self.events.append(done)
            subscribers, self._subscribers = list(self._subscribers), []
        for q in subscribers:
            q.put(done)

    def subscribe(self):
        """Queue pre-filled with history, then live events."""
        q = queue.Queue()
        with self._lock:
            for e in self.events:
                q.put(e)
            if self.status == "running":
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


def create_run(kind, user_id=None, key=None):
    key = key or kind
    with _lock:
        if key in _active:
            raise AlreadyRunning(_active[key].id)
        with db.session() as conn:
            cur = conn.execute("INSERT INTO runs(kind, status, started_at, stats, log, requested_by) "
                               "VALUES(?, 'running', ?, '{}', '', ?)", (kind, db.now_iso(), user_id))
            run = Run(cur.lastrowid, kind, key)
        _active[key] = run
        _recent[run.id] = run
        for old in sorted(_recent)[:-_RECENT_MAX]:
            _recent.pop(old, None)
    return run


def start(kind, target, user_id=None, key=None, **kwargs):
    """Create a run and execute target(run, **kwargs) in a daemon thread. target returns stats."""
    run = create_run(kind, user_id, key)

    def body():
        try:
            stats = target(run, **kwargs)
            run.finish("done", stats if isinstance(stats, dict) else None)
        except Exception as exc:  # noqa: BLE001 — any crash ends the run as failed
            log.error("jobs run %s crashed: %s\n%s", run.id, exc, traceback.format_exc())
            run.log(f"failed: {exc}")
            run.finish("failed")

    threading.Thread(target=body, name=f"jobs-run-{run.id}", daemon=True).start()
    return run


def run_inline(kind, target, user_id=None, key=None, **kwargs):
    """Same as start() but in the calling thread (CLI)."""
    run = create_run(kind, user_id, key)
    try:
        stats = target(run, **kwargs)
        run.finish("done", stats if isinstance(stats, dict) else None)
    except Exception:
        run.finish("failed")
        raise
    return run


def running_id(kind):
    with _lock:
        run = _active.get(kind)
        return run.id if run else None


def live(run_id):
    return _recent.get(run_id)


def run_dict(row):
    return {"id": row["id"], "kind": row["kind"], "status": row["status"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "stats": db.loads(row["stats"], {})}


def mark_interrupted(conn):
    """At startup: runs left 'running' by a previous process can never finish."""
    conn.execute("UPDATE runs SET status='failed', finished_at=?, log=COALESCE(log,'') || 'interrupted by restart\n' "
                 "WHERE status='running'", (db.now_iso(),))
