#!/usr/bin/env python3
"""
Self-contained background deploy for the freight-api on the Surface (Windows).

Run modes:
  python _deploy_surface.py            -> spawns itself detached, returns immediately
  python _deploy_surface.py run        -> does the actual work (the detached worker)

It installs a portable Node + PostgreSQL into the runner workspace, builds the
NestJS monorepo, applies the drizzle schema with psql, seeds, starts the API
detached on :3001, and registers both Postgres and the API in data/services.json
so run-server.ps1 keeps them alive and reboot-durable. All progress is written to
_deploy.status next to this file (poll that for status). Idempotent.
"""
import os, sys, json, time, zipfile, secrets, subprocess, urllib.request
from pathlib import Path

NODE_VER = "20.18.0"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VER}/node-v{NODE_VER}-win-x64.zip"
PG_URL   = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"
PG_PORT  = "5433"
API_PORT = "3001"
DB_URL   = f"postgres://postgres@127.0.0.1:{PG_PORT}/factoring"

HERE   = Path(__file__).resolve().parent              # services/freight-api (monorepo root)
MWB    = HERE.parents[1]                               # mw-backend
WORK   = MWB / "data" / "runner-workspace"            # persistent, gitignored
TOOLS  = WORK / "tools"
PGDATA = TOOLS / "pgdata"
STATUS = HERE / "_deploy.status"
SERVICES = MWB / "data" / "services.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    with open(STATUS, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def fail(step, err):
    log(f"FAILED [{step}]: {err}")
    sys.exit(1)


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return
    log(f"downloading {url.split('/')[-1]} ...")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def unzip(zip_path, dest):
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)


def run(cmd, cwd=None, env=None, timeout=1200, check=True, step="run"):
    log("$ " + (" ".join(cmd) if isinstance(cmd, list) else cmd))
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, shell=isinstance(cmd, str))
    if p.returncode != 0:
        tail = (p.stdout or "")[-600:] + "\n" + (p.stderr or "")[-1200:]
        if check:
            fail(step, f"exit {p.returncode}\n{tail}")
        else:
            log(f"(non-fatal) {step} exit {p.returncode}: {tail[-300:]}")
    return p


def start_detached(cmd, cwd, env, logfile):
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    lf = open(logfile, "ab")
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=lf, stderr=lf,
                            stdin=subprocess.DEVNULL, close_fds=True, creationflags=flags)


def register_service(entry):
    SERVICES.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(SERVICES.read_text()) if SERVICES.exists() else []
        if isinstance(data, dict):
            data = [data]
    except Exception:
        data = []
    data = [s for s in data if s.get("name") != entry["name"]] + [entry]
    SERVICES.write_text(json.dumps(data, indent=2))


def deploy():
    STATUS.write_text("")  # reset
    log("=== freight-api deploy starting ===")
    TOOLS.mkdir(parents=True, exist_ok=True)

    # 1) Node (portable) ------------------------------------------------------
    node_home = TOOLS / f"node-v{NODE_VER}-win-x64"
    node_exe = node_home / "node.exe"
    if not node_exe.exists():
        z = TOOLS / "node.zip"
        try:
            download(NODE_URL, z); unzip(z, TOOLS)
        except Exception as e:
            fail("node-install", e)
    log(f"node: {node_exe}")

    env = os.environ.copy()
    env["PATH"] = f"{node_home};{node_home / 'node_modules' / 'npm' / 'bin'};" + env.get("PATH", "")
    env["DATABASE_URL"] = DB_URL
    env["NEST_PORT"] = API_PORT
    env.setdefault("JWT_SECRET", secrets.token_hex(24))
    npm = str(node_home / "npm.cmd")

    # corepack -> pnpm
    run([str(node_home / "corepack.cmd"), "enable"], env=env, check=False, step="corepack-enable")
    run([str(node_home / "corepack.cmd"), "prepare", "pnpm@11.7.0", "--activate"], env=env, check=False, step="corepack-pnpm")
    pnpm = str(node_home / "pnpm.cmd")
    if not Path(pnpm).exists():
        run([npm, "i", "-g", "pnpm@11.7.0"], env=env, step="npm-i-pnpm")
        pnpm = str(node_home / "pnpm.cmd")

    # 2) PostgreSQL (portable) ------------------------------------------------
    pg_bin = TOOLS / "pgsql" / "bin"
    if not (pg_bin / "pg_ctl.exe").exists():
        z = TOOLS / "pg.zip"
        try:
            download(PG_URL, z); unzip(z, TOOLS)  # contains pgsql/
        except Exception as e:
            fail("pg-install", e)
    log(f"postgres: {pg_bin}")

    if not (PGDATA / "PG_VERSION").exists():
        run([str(pg_bin / "initdb.exe"), "-D", str(PGDATA), "-U", "postgres",
             "--auth=trust", "--encoding=UTF8"], env=env, step="initdb")
    # start postgres (idempotent: pg_ctl start is a no-op error if already running)
    run([str(pg_bin / "pg_ctl.exe"), "-D", str(PGDATA),
         "-o", f"-p {PG_PORT}", "-l", str(TOOLS / "pg.log"), "start"],
        env=env, timeout=120, check=False, step="pg-start")
    time.sleep(3)
    run([str(pg_bin / "createdb.exe"), "-h", "127.0.0.1", "-p", PG_PORT, "-U", "postgres", "factoring"],
        env=env, check=False, step="createdb")

    # 3) Build the monorepo ---------------------------------------------------
    run([pnpm, "install", "--frozen-lockfile"], cwd=str(HERE), env=env, timeout=1500, step="pnpm-install")
    # schema -> SQL -> apply with psql (non-interactive)
    dbdir = HERE / "packages" / "db"
    run([pnpm, "exec", "drizzle-kit", "generate"], cwd=str(dbdir), env=env, check=False, step="drizzle-generate")
    sqls = sorted((dbdir / "drizzle").glob("*.sql")) if (dbdir / "drizzle").exists() else []
    psql = str(pg_bin / "psql.exe")
    for s in sqls:
        run([psql, "-h", "127.0.0.1", "-p", PG_PORT, "-U", "postgres", "-d", "factoring",
             "-v", "ON_ERROR_STOP=0", "-f", str(s)], env=env, check=False, step=f"psql:{s.name}")
    # build api
    run([pnpm, "-C", "apps/api", "run", "build"], cwd=str(HERE), env=env, timeout=900, step="build-api")
    # seed (non-fatal: demo can run with empty tables if seed has issues)
    run([pnpm, "-C", "apps/api", "run", "seed"], cwd=str(HERE), env=env, timeout=300, check=False, step="seed")

    # 4) Start the API detached + register for durability ---------------------
    api_dir = HERE / "apps" / "api"
    main_js = api_dir / "dist" / "apps" / "api" / "src" / "main.js"
    if not main_js.exists():
        # fall back to whatever main.js the build produced
        found = list((api_dir / "dist").rglob("main.js"))
        if not found:
            fail("locate-main", "no dist/**/main.js after build")
        main_js = found[0]
    # env-injecting launcher so run-server.ps1 needs no per-service env support
    runner_js = api_dir / "_run.js"
    runner_js.write_text(
        f"process.env.DATABASE_URL={json.dumps(DB_URL)};\n"
        f"process.env.NEST_PORT={json.dumps(API_PORT)};\n"
        f"process.env.JWT_SECRET={json.dumps(env['JWT_SECRET'])};\n"
        f"require({json.dumps(str(main_js))});\n"
    )
    start_detached([str(node_exe), str(runner_js)], cwd=str(api_dir), env=env,
                   logfile=str(TOOLS / "freight-api.log"))

    # 5) Health check ---------------------------------------------------------
    import socket
    ok = False
    for _ in range(30):
        s = socket.socket(); s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", int(API_PORT))); ok = True; s.close(); break
        except Exception:
            s.close(); time.sleep(2)
    if not ok:
        fail("api-health", f"not listening on {API_PORT} after build; see data/runner-workspace/tools/freight-api.log")

    # durability (only once healthy): run-server.ps1 keeps these up + reboot-safe
    register_service({"name": "freight-pg", "cmd": str(pg_bin / "pg_ctl.exe"),
                      "args": f'-D "{PGDATA}" -o "-p {PG_PORT}" -l "{TOOLS / "pg.log"}" start',
                      "cwd": str(TOOLS), "port": int(PG_PORT)})
    register_service({"name": "freight-api", "cmd": str(node_exe),
                      "args": f'"{runner_js}"', "cwd": str(api_dir), "port": int(API_PORT)})
    log(f"DONE: freight-api LISTENING on 127.0.0.1:{API_PORT}; services registered for durability.")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        try:
            deploy()
        except Exception as e:
            fail("worker", repr(e))
        return
    # spawn the worker detached and return immediately
    flags = 0x00000008 | 0x00000200
    STATUS.write_text(time.strftime('%H:%M:%S') + "  triggered; worker starting...\n")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    print("deploy worker triggered; poll _deploy.status")


if __name__ == "__main__":
    main()
