"""Environment-driven settings (contract §1).

Values are read through functions rather than module constants so tests (and a
late `load_dotenv()`) can change the environment after import.
"""
import os
import socket
import subprocess
from functools import lru_cache
from pathlib import Path

PROTOCOL = 1  # worker ⇄ primary protocol version; bump on incompatible changes

PKG_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PKG_DIR.parent
PACKAGE_DATA_DIR = PKG_DIR / "data"  # read-only data shipped with the code (gazetteer)

# Ashley's home — the default centre for "local" and gem scoring.
HOME_LAT, HOME_LNG = 45.0619, -92.9766
HOME_LABEL = "Birchwood Village, MN"

# Function keywords used to search large ATS tenants (Workday, Oracle, ...).
FUNCTION_KEYWORDS = ["marketing", "communications", "brand", "content", "public relations"]

LOCAL_STATES = ("MN", "WI")


def _env(name, default=None):
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def data_dir() -> Path:
    """Where jobscout.db, mw.db and .secret_key live (JOBSCOUT_DATA_DIR overrides)."""
    path = Path(_env("JOBSCOUT_DATA_DIR", str(BACKEND_DIR / "data")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "jobscout.db"


def users_db_path() -> Path:
    return data_dir() / "mw.db"


def secret_path() -> Path:
    return data_dir() / ".secret_key"


def role() -> str:
    return _env("MW_ROLE", "primary").lower()


def instance() -> str:
    return _env("MW_INSTANCE") or socket.gethostname()


def primary_url() -> str:
    return _env("MW_PRIMARY_URL", "https://api.michaelwegter.com").rstrip("/")


def worker_token():
    return _env("JOBS_WORKER_TOKEN")


def allowed_emails() -> set:
    raw = _env("JOBS_ALLOWED_EMAILS", "zweetztuph@gmail.com")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def scheduler_enabled() -> bool:
    return _env("JOBS_SCHEDULER", "1") != "0"


def sweep_hour() -> int:
    try:
        return max(0, min(23, int(_env("JOBS_SWEEP_HOUR", "2"))))
    except ValueError:
        return 2


def ai_api_base() -> str:
    return _env("JOBS_AI_API_BASE", "http://localhost:1234/v1").rstrip("/")


def ai_model() -> str:
    return _env("JOBS_AI_MODEL", "google/gemma-4-12b")


def ai_api_key() -> str:
    return _env("JOBS_AI_API_KEY", "lm-studio")


def ai_local() -> bool:
    return _env("JOBS_AI_LOCAL", "0") == "1"


def ors_api_key():
    return _env("ORS_API_KEY")


def browser_channel() -> str:
    return _env("JOBS_BROWSER_CHANNEL", "chrome")


def google_places_api_key():
    return _env("GOOGLE_PLACES_API_KEY")


@lru_cache(maxsize=1)
def commit_sha() -> str:
    """Short git SHA of the running checkout, cached; "unknown" when git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(BACKEND_DIR),
            capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and sha else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
