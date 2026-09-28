"""AI worker loop (MW_ROLE=ai-worker on wegter-pc, or in-process with JOBS_AI_LOCAL=1).

Every 5 s while busy (30 s when idle): heartbeat → if LM Studio answers, claim up to 4 tasks →
run each through ai.chat_json → complete/fail. A protocol mismatch stops claiming (heartbeats
continue so the UI can show "needs git pull").

    python -m jobscout.worker --once          one pass against MW_PRIMARY_URL
    python -m jobscout.worker --once --local  one pass against the local database
"""
import argparse
import logging
import threading

import requests

from . import ai, config, db, tasks

log = logging.getLogger("jobscout")

BUSY_SLEEP, IDLE_SLEEP = 5, 30
BATCH = 4
KINDS = list(tasks.KIND_PRIORITY)


class ProtocolMismatch(Exception):
    pass


class RemoteTransport:
    """Talks to the primary over HTTPS with X-Worker-Token."""

    def __init__(self, base=None, token=None, session=None):
        self.base = (base or config.primary_url()).rstrip("/")
        self.token = token or config.worker_token() or ""
        self.session = session or requests.Session()

    def _post(self, path, body):
        resp = self.session.post(f"{self.base}/jobs/worker/{path}", json=body, timeout=30,
                                 headers={"X-Worker-Token": self.token})
        if resp.status_code == 409 and (resp.json() or {}).get("error") == "protocol_mismatch":
            raise ProtocolMismatch(resp.json().get("protocol"))
        resp.raise_for_status()
        return resp.json()

    def heartbeat(self, info):
        return self._post("heartbeat", info)

    def claim(self, worker_id, kinds, max_n):
        return self._post("claim", {"worker_id": worker_id, "kinds": kinds, "max": max_n,
                                    "protocol": config.PROTOCOL})["tasks"]

    def complete(self, worker_id, task_id, result, model):
        self._post("complete", {"task_id": task_id, "worker_id": worker_id, "result": result, "model": model})

    def fail(self, worker_id, task_id, error):
        self._post("fail", {"task_id": task_id, "worker_id": worker_id, "error": error})


class LocalTransport:
    """Same operations directly against the local database (primary with JOBS_AI_LOCAL=1, CLI)."""

    def heartbeat(self, info):
        with db.session() as conn:
            record_heartbeat(conn, info)
        return {"ok": True, "protocol": config.PROTOCOL}

    def claim(self, worker_id, kinds, max_n):
        with db.session() as conn:
            return tasks.claim(conn, worker_id, kinds, max_n)

    def complete(self, worker_id, task_id, result, model):
        with db.session() as conn:
            tasks.complete(conn, task_id, worker_id, result, model)

    def fail(self, worker_id, task_id, error):
        with db.session() as conn:
            tasks.fail(conn, task_id, worker_id, error)


def record_heartbeat(conn, info):
    """Upsert a workers row from a heartbeat body (shared by the API and LocalTransport)."""
    worker_id = str(info.get("worker_id") or info.get("instance") or "worker")[:80]
    conn.execute(
        "INSERT INTO workers(worker_id, instance, role, model, lmstudio_ok, commit_sha, protocol, last_heartbeat_at, "
        "tasks_done_today, day) VALUES(?,?,?,?,?,?,?,?,0,NULL) ON CONFLICT(worker_id) DO UPDATE SET "
        "instance=excluded.instance, role=excluded.role, model=excluded.model, lmstudio_ok=excluded.lmstudio_ok, "
        "commit_sha=excluded.commit_sha, protocol=excluded.protocol, last_heartbeat_at=excluded.last_heartbeat_at",
        (worker_id, str(info.get("instance") or worker_id)[:80], str(info.get("role") or "ai-worker")[:40],
         str(info.get("model") or "")[:120], 1 if info.get("lmstudio_ok") else 0, str(info.get("commit") or "")[:40],
         int(info.get("protocol") or 0), db.now_iso()))


class Worker:
    def __init__(self, transport, worker_id=None, ai_session=None):
        self.transport = transport
        self.worker_id = worker_id or config.instance()
        self.ai_session = ai_session
        self.mismatch_logged = False

    def run_once(self):
        """One heartbeat + claim/score pass → stats dict."""
        ok, models = ai.health(session=self.ai_session)
        model = config.ai_model()
        stats = {"lmstudio_ok": ok, "claimed": 0, "done": 0, "failed": 0, "protocol_mismatch": False}
        info = {"worker_id": self.worker_id, "instance": config.instance(), "model": model, "lmstudio_ok": ok,
                "commit": config.commit_sha(), "protocol": config.PROTOCOL,
                "role": "ai-worker" if config.role() == "ai-worker" else "ai-worker-local"}
        try:
            self.transport.heartbeat(info)
        except (requests.RequestException, ValueError) as exc:
            log.warning("worker: heartbeat failed: %s", exc)
            return stats
        if not ok:
            return stats
        try:
            claimed = self.transport.claim(self.worker_id, KINDS, BATCH)
        except ProtocolMismatch as exc:
            if not self.mismatch_logged:
                log.error("worker: protocol mismatch (primary %s, worker %s) — git pull + restart needed",
                          exc, config.PROTOCOL)
                self.mismatch_logged = True
            stats["protocol_mismatch"] = True
            return stats
        except (requests.RequestException, ValueError) as exc:
            log.warning("worker: claim failed: %s", exc)
            return stats
        stats["claimed"] = len(claimed)
        for task in claimed:
            try:
                result, used_model = ai.chat_json(task["kind"], task["payload"], session=self.ai_session)
            except Exception as exc:  # noqa: BLE001 — model/HTTP/parse error: report, next task
                stats["failed"] += 1
                log.warning("worker: task %s (%s) failed: %s", task["id"], task["kind"], exc)
                try:
                    self.transport.fail(self.worker_id, task["id"], f"{type(exc).__name__}: {exc}"[:500])
                except Exception as inner:  # noqa: BLE001
                    log.warning("worker: could not report failure: %s", inner)
                continue
            try:
                self.transport.complete(self.worker_id, task["id"], result, used_model)
                stats["done"] += 1
            except Exception as exc:  # noqa: BLE001 — the primary records invalid results itself;
                stats["failed"] += 1  # anything else is retried when the lease expires
                log.warning("worker: completing task %s rejected: %s", task["id"], exc)
        return stats

    def loop(self, stop=None):
        stop = stop or threading.Event()
        log.info("worker %s started (protocol %s)", self.worker_id, config.PROTOCOL)
        while not stop.is_set():
            try:
                stats = self.run_once()
            except Exception as exc:  # noqa: BLE001 — never let the thread die
                log.warning("worker: pass crashed: %s", exc)
                stats = {"claimed": 0}
            stop.wait(BUSY_SLEEP if stats.get("claimed") else IDLE_SLEEP)


_thread = None


def start_worker_thread(local=False):
    """Start the worker loop once per process (daemon thread)."""
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    transport = LocalTransport() if local else RemoteTransport()
    worker_id = f"{config.instance()}-local" if local else config.instance()
    _thread = threading.Thread(target=Worker(transport, worker_id).loop, name="jobs-ai-worker", daemon=True)
    _thread.start()
    return _thread


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m jobscout.worker")
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument("--local", action="store_true", help="use the local database instead of MW_PRIMARY_URL")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.local:
        db.init()
    worker = Worker(LocalTransport() if args.local else RemoteTransport(),
                    f"{config.instance()}-local" if args.local else None)
    if args.once:
        print(worker.run_once())
    else:
        worker.loop()


if __name__ == "__main__":
    main()
